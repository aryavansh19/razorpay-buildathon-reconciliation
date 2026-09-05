"""Deterministic matching passes.

The passes run strictest first. That ordering is the whole design: an earlier pass
matches on evidence that cannot reasonably be coincidence (an exact settlement
reference and an exact amount), and each later pass relaxes exactly one dimension.
By the time a looser pass runs, everything it could have stolen from a stricter
pass is already consumed, so relaxing a constraint can only add matches the
stricter evidence could not explain.

Running them in the opposite order would produce a similar headline match rate
built out of worse matches, which is the failure this ordering exists to prevent.

Pass ladder, settlement to bank
-------------------------------
0. Suppress duplicate re-posted credits before matching begins.
1. Reference and amount both exact.
2. Reference exact, amount within tolerance (absorbs the bank's own rounding).
3. No usable reference, amount exact, inside the lag window, exactly one candidate.
4. No usable reference, amount within tolerance, one candidate.
5. Sweep credits, resolved by bounded subset-sum over the remaining settlements.

Pass ladder, payment to order
-----------------------------
A. The payment carries an order reference. Authoritative.
B. No reference: amount and time window, accepted only when unique.

What is deliberately absent
---------------------------
There is no pass that picks the "closest" or "most likely" candidate from several.
Every pass either finds exactly one explanation or declines. Declining costs a
match; guessing costs correctness, and a wrong match closes a break that a human
would otherwise have caught.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .audit import Action, AuditLog
from .models import (
    BankTxn,
    Ledger,
    Match,
    MatchKind,
    OrderStatus,
    ReasonCode,
    ReconException,
    Settlement,
    Tier,
    Tolerance,
    rupees,
)
from .subsetsum import find_subsets
from .verify import Assignments, Verifier


@dataclass
class PassStats:
    """What one pass did, for the per-pass table in the report."""

    name: str
    considered: int = 0
    accepted: int = 0
    rejected: int = 0
    declined_ambiguous: int = 0
    elapsed_ms: float = 0.0
    notes: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str, amount: int = 1) -> None:
        self.notes[key] = self.notes.get(key, 0) + amount


@dataclass
class PassContext:
    """Shared state threaded through every pass."""

    ledger: Ledger
    tolerance: Tolerance
    verifier: Verifier
    assignments: Assignments
    audit: AuditLog
    matches: list[Match] = field(default_factory=list)
    exceptions: list[ReconException] = field(default_factory=list)
    # Subjects that already have a final finding. The residue stage skips these so
    # a record cannot be raised twice under two different reasons.
    adjudicated: set[str] = field(default_factory=set)

    def accept(self, match: Match, actor: str) -> bool:
        """Route a proposal through the verifier. The only way to gain a match."""
        result = self.verifier.accept(match, self.assignments)
        if result.ok:
            self.matches.append(match)
            self.audit.record(
                actor,
                Action.MATCH_ACCEPTED,
                match.left_id,
                {
                    "kind": match.kind.value,
                    "left_id": match.left_id,
                    "right_ids": list(match.right_ids),
                    "tier": match.tier.value,
                    "pass": match.pass_name,
                    "delta_paise": match.delta_paise,
                    "checks": result.checks,
                },
            )
        else:
            self.audit.record(
                actor,
                Action.MATCH_REJECTED,
                match.left_id,
                {
                    "kind": match.kind.value,
                    "left_id": match.left_id,
                    "right_ids": list(match.right_ids),
                    "pass": match.pass_name,
                    "failure": result.failure,
                    "checks": result.checks,
                },
            )
        return result.ok

    def raise_finding(self, exception: ReconException, actor: str) -> None:
        """Record an informational finding that does not close the record.

        Used for discrepancies that coexist with a successful match, such as a
        settlement whose money reconciles against the bank while its reported
        header figure is wrong. The record still needs to flow through matching and
        residue triage, so it is deliberately not marked adjudicated.
        """
        self._append_exception(exception, actor)

    def raise_exception(self, exception: ReconException, actor: str) -> None:
        """Record a terminal finding. The record is closed to further triage."""
        self.adjudicated.add(exception.subject_id)
        self._append_exception(exception, actor)

    def _append_exception(self, exception: ReconException, actor: str) -> None:
        self.exceptions.append(exception)
        self.audit.record(
            actor,
            Action.EXCEPTION_RAISED,
            exception.subject_id,
            {
                "kind": exception.kind.value,
                "reason_code": exception.reason_code.value,
                "reason": exception.reason,
                "amount_paise": exception.amount_paise,
                "needs_human": exception.needs_human,
                "suggested_action": exception.suggested_action,
            },
        )

    # -- shared candidate selection ---------------------------------------

    def open_credits(self) -> list[BankTxn]:
        return sorted(
            (
                txn
                for txn in self.ledger.credits()
                if self.assignments.bank_is_open(txn.bank_txn_id)
            ),
            key=lambda t: (t.value_date, t.bank_txn_id),
        )

    def open_settlements_near(self, txn: BankTxn) -> list[Settlement]:
        """Settlements that could plausibly have been paid by this credit."""
        return sorted(
            (
                settlement
                for settlement in self.ledger.settlements.values()
                if self.assignments.settlement_is_open(settlement.settlement_id)
                and self.tolerance.within_lag(settlement.settled_at, txn.value_date)
            ),
            key=lambda s: (s.settled_at, s.settlement_id),
        )


# ---------------------------------------------------------------------------
# Pass 0a: settlement header against its own line items
# ---------------------------------------------------------------------------


def audit_settlement_net_identity(ctx: PassContext) -> PassStats:
    """Check every settlement header against the netting identity.

    This is not a matching pass and it consumes nothing. It answers a question
    matching never asks: does the gateway's own reported net agree with the sum of
    the line items it attributed to that settlement?

    When it does not, the money is usually still correct. The bank credit follows
    the line items, so the settlement reconciles cleanly against the statement and
    a matching-only pipeline reports a perfect run while the figure the merchant
    books into its accounts is wrong. That is precisely the break worth catching,
    and it is invisible to anything that trusts the header.

    Because the record still needs to go through matching, the discrepancy is
    recorded as a finding rather than a terminal exception.
    """
    stats = PassStats(name="0a_settlement_net_identity")
    started = time.perf_counter()

    for settlement_id, settlement in sorted(ctx.ledger.settlements.items()):
        stats.considered += 1
        recomputed = ctx.ledger.computed_net_paise(settlement_id)
        delta = settlement.net_paise - recomputed
        if ctx.tolerance.within_amount(delta):
            continue

        lines = ctx.ledger.lines_for_settlement(settlement_id)
        ctx.raise_finding(
            ReconException(
                kind=MatchKind.SETTLEMENT_BANK,
                subject_id=settlement_id,
                reason_code=ReasonCode.NET_IDENTITY_BREAK,
                reason=(
                    f"Gateway reports a net of {rupees(settlement.net_paise)} but the "
                    f"{len(lines['payments'])} payment, {len(lines['refunds'])} refund, "
                    f"{len(lines['chargebacks'])} chargeback and "
                    f"{len(lines['adjustments'])} adjustment lines attributed to it sum "
                    f"to {rupees(recomputed)}, a difference of {rupees(abs(delta))}"
                ),
                amount_paise=abs(delta),
                suggested_action=(
                    "Book the recomputed figure, not the reported one, and raise the "
                    "discrepancy with the gateway. Matching is unaffected: the bank "
                    "credit follows the line items."
                ),
                needs_human=True,
                evidence={
                    "reported_net_paise": settlement.net_paise,
                    "recomputed_net_paise": recomputed,
                    "delta_paise": delta,
                    "line_counts": {name: len(values) for name, values in lines.items()},
                },
            ),
            stats.name,
        )
        stats.accepted += 1
        stats.bump("over_reported" if delta > 0 else "under_reported")

    stats.elapsed_ms = (time.perf_counter() - started) * 1000
    return stats


# ---------------------------------------------------------------------------
# Pass 0: duplicate suppression
# ---------------------------------------------------------------------------


def suppress_duplicate_credits(ctx: PassContext) -> PassStats:
    """Remove re-posted credits from matching before any pass runs.

    A bank occasionally posts the same credit twice. Both copies carry identical
    amount, value date and narration, which means identical reference. Two
    genuinely different settlements cannot produce that, because their references
    differ, so identical narration is strong evidence of a re-post rather than a
    coincidence.

    This runs first rather than last on purpose. Left in the pool, the second copy
    is available to a later, looser pass, which would match it to some other
    settlement and double-count revenue. Suppressing up front makes that
    impossible.

    The first copy by identifier is kept and the rest are raised. Which copy is
    retained is arbitrary and does not affect the money; what matters is that
    exactly one survives.
    """
    stats = PassStats(name="0_suppress_duplicate_credits")
    started = time.perf_counter()

    groups: dict[tuple[int, str, str], list[BankTxn]] = {}
    for txn in ctx.ledger.credits():
        key = (txn.amount_paise, txn.value_date.isoformat(), txn.narration)
        groups.setdefault(key, []).append(txn)

    for group in groups.values():
        stats.considered += len(group)
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda t: t.bank_txn_id)
        keeper, duplicates = ordered[0], ordered[1:]
        for duplicate in duplicates:
            ctx.assignments.suppressed_bank.add(duplicate.bank_txn_id)
            ctx.audit.record(
                stats.name,
                Action.CREDIT_SUPPRESSED,
                duplicate.bank_txn_id,
                {"duplicate_of": keeper.bank_txn_id, "amount_paise": duplicate.amount_paise},
            )
            has_reference = duplicate.utr_hint is not None
            ctx.raise_exception(
                ReconException(
                    kind=MatchKind.SETTLEMENT_BANK,
                    subject_id=duplicate.bank_txn_id,
                    reason_code=ReasonCode.DUPLICATE_BANK_CREDIT,
                    reason=(
                        f"Identical to {keeper.bank_txn_id}: same amount "
                        f"{rupees(duplicate.amount_paise)}, same value date "
                        f"{duplicate.value_date}, same narration"
                        + (
                            f", same reference {duplicate.utr_hint}"
                            if has_reference
                            else " (narration carries no reference, so this is "
                            "strong but not conclusive)"
                        )
                    ),
                    amount_paise=duplicate.amount_paise,
                    suggested_action=(
                        "Confirm with the bank that this is a re-post and not a "
                        "second genuine transfer, then exclude it from revenue."
                    ),
                    needs_human=True,
                ),
                stats.name,
            )
            stats.accepted += 1
            stats.bump("with_reference" if has_reference else "without_reference")

    stats.elapsed_ms = (time.perf_counter() - started) * 1000
    return stats


# ---------------------------------------------------------------------------
# Passes 1 and 2: settlement reference
# ---------------------------------------------------------------------------


def _reference_index(ctx: PassContext) -> dict[str, str]:
    return {s.utr: s.settlement_id for s in ctx.ledger.settlements.values()}


def _match_by_reference(ctx: PassContext, stats: PassStats, require_exact: bool) -> PassStats:
    started = time.perf_counter()
    index = _reference_index(ctx)

    for txn in ctx.open_credits():
        if txn.utr_hint is None:
            continue
        settlement_id = index.get(txn.utr_hint)
        if settlement_id is None:
            # The narration exposed something reference-shaped that belongs to no
            # settlement, usually a reference truncated by the remitting bank.
            # Falling through to the amount passes is the whole reason a reference
            # miss must not be terminal.
            stats.bump("reference_not_in_ledger")
            continue
        if not ctx.assignments.settlement_is_open(settlement_id):
            stats.bump("settlement_already_matched")
            continue

        stats.considered += 1
        recomputed = ctx.ledger.computed_net_paise(settlement_id)
        delta = txn.amount_paise - recomputed
        if require_exact and delta != 0:
            stats.bump("deferred_to_tolerance_pass")
            continue
        if not require_exact and delta == 0:
            # Already handled by the exact pass; counting it here would
            # double-report the same work.
            continue

        match = Match(
            kind=MatchKind.SETTLEMENT_BANK,
            left_id=txn.bank_txn_id,
            right_ids=(settlement_id,),
            tier=Tier.AUTO,
            pass_name=stats.name,
            delta_paise=delta,
            confidence=1.0 if require_exact else 0.97,
            evidence={
                "reference": txn.utr_hint,
                "recomputed_net_paise": recomputed,
                "credit_paise": txn.amount_paise,
            },
        )
        if ctx.accept(match, stats.name):
            stats.accepted += 1
            if delta:
                stats.bump("absorbed_rounding_drift")
        else:
            stats.rejected += 1

    stats.elapsed_ms = (time.perf_counter() - started) * 1000
    return stats


def match_reference_exact(ctx: PassContext) -> PassStats:
    """Reference matches and the recomputed net equals the credit to the paise."""
    return _match_by_reference(
        ctx, PassStats(name="1_reference_and_amount_exact"), require_exact=True
    )


def match_reference_tolerance(ctx: PassContext) -> PassStats:
    """Reference matches, amount differs by no more than the declared tolerance."""
    return _match_by_reference(
        ctx, PassStats(name="2_reference_amount_within_tolerance"), require_exact=False
    )


# ---------------------------------------------------------------------------
# Passes 3 and 4: amount and date window, no usable reference
# ---------------------------------------------------------------------------


def _match_by_amount_window(
    ctx: PassContext, stats: PassStats, tolerance_paise: int
) -> PassStats:
    started = time.perf_counter()

    for txn in ctx.open_credits():
        candidates = [
            settlement
            for settlement in ctx.open_settlements_near(txn)
            if abs(txn.amount_paise - ctx.ledger.computed_net_paise(settlement.settlement_id))
            <= tolerance_paise
        ]
        if not candidates:
            continue
        stats.considered += 1

        if len(candidates) > 1:
            # Several settlements in the window share this amount. Declining is
            # the correct outcome: any choice would be a guess, and the sweep pass
            # or a human can still resolve it.
            stats.declined_ambiguous += 1
            stats.bump("multiple_candidates")
            continue

        settlement = candidates[0]
        recomputed = ctx.ledger.computed_net_paise(settlement.settlement_id)
        delta = txn.amount_paise - recomputed
        if tolerance_paise > 0 and delta == 0:
            # The exact pass already took this one.
            continue

        match = Match(
            kind=MatchKind.SETTLEMENT_BANK,
            left_id=txn.bank_txn_id,
            right_ids=(settlement.settlement_id,),
            tier=Tier.AUTO,
            pass_name=stats.name,
            delta_paise=delta,
            confidence=0.90 if tolerance_paise == 0 else 0.82,
            evidence={
                "reference": txn.utr_hint,
                "recomputed_net_paise": recomputed,
                "credit_paise": txn.amount_paise,
                "value_date": txn.value_date.isoformat(),
                "settled_at": settlement.settled_at.isoformat(),
                "sole_candidate_in_window": True,
            },
        )
        if ctx.accept(match, stats.name):
            stats.accepted += 1
            if txn.utr_hint is None:
                stats.bump("no_reference_in_narration")
        else:
            stats.rejected += 1

    stats.elapsed_ms = (time.perf_counter() - started) * 1000
    return stats


def match_amount_window_exact(ctx: PassContext) -> PassStats:
    return _match_by_amount_window(
        ctx, PassStats(name="3_amount_exact_in_window"), tolerance_paise=0
    )


def match_amount_window_tolerance(ctx: PassContext) -> PassStats:
    return _match_by_amount_window(
        ctx,
        PassStats(name="4_amount_within_tolerance_in_window"),
        tolerance_paise=ctx.tolerance.amount_paise,
    )


# ---------------------------------------------------------------------------
# Pass 5: sweep credits via bounded subset-sum
# ---------------------------------------------------------------------------


def match_sweep_subset_sum(ctx: PassContext) -> PassStats:
    """Resolve credits that collapse several settlements into one line.

    Only reached by credits no single settlement explains. The search is bounded
    three ways (candidate window, cardinality cap, node budget) and its
    non-answers are reported rather than swallowed: ambiguity and budget
    exhaustion each become their own exception, because "we could not tell" and
    "we ran out of time" are different problems for whoever picks this up.
    """
    stats = PassStats(name="5_sweep_subset_sum")
    started = time.perf_counter()

    for txn in ctx.open_credits():
        candidates = ctx.open_settlements_near(txn)
        # Subset-sum requires strictly positive values. A settlement whose net is
        # zero or negative was never paid out, so excluding it is correct on the
        # domain as well as required by the solver.
        payable = [
            settlement
            for settlement in candidates
            if ctx.ledger.computed_net_paise(settlement.settlement_id) > 0
        ]
        if len(payable) < 2:
            continue

        stats.considered += 1
        values = [
            ctx.ledger.computed_net_paise(settlement.settlement_id) for settlement in payable
        ]
        result = find_subsets(
            values,
            txn.amount_paise,
            tolerance=ctx.tolerance.amount_paise,
            min_size=2,
            max_size=ctx.tolerance.max_sweep_size,
            node_budget=ctx.tolerance.subset_sum_node_budget,
            max_solutions=2,
        )
        stats.bump("nodes_expanded", result.nodes_expanded)

        if result.budget_exceeded:
            stats.bump("budget_exceeded")
            ctx.raise_exception(
                ReconException(
                    kind=MatchKind.SETTLEMENT_BANK,
                    subject_id=txn.bank_txn_id,
                    reason_code=ReasonCode.SUBSET_SUM_BUDGET_EXCEEDED,
                    reason=(
                        f"Sweep search over {len(payable)} candidate settlements "
                        f"exceeded the {ctx.tolerance.subset_sum_node_budget} node "
                        f"budget for credit {rupees(txn.amount_paise)}"
                    ),
                    amount_paise=txn.amount_paise,
                    suggested_action=(
                        "Narrow the window or raise the node budget, then re-run. "
                        "Do not assume this credit is unattributed."
                    ),
                ),
                stats.name,
            )
            continue

        if result.is_ambiguous:
            stats.declined_ambiguous += 1
            options = [
                [payable[i].settlement_id for i in solution] for solution in result.solutions
            ]
            ctx.raise_exception(
                ReconException(
                    kind=MatchKind.SETTLEMENT_BANK,
                    subject_id=txn.bank_txn_id,
                    reason_code=ReasonCode.AMBIGUOUS_CANDIDATES,
                    reason=(
                        f"Credit {rupees(txn.amount_paise)} is explained by more than "
                        f"one combination of settlements: {options}"
                    ),
                    amount_paise=txn.amount_paise,
                    suggested_action=(
                        "Ask the bank for the sweep composition. Do not pick one "
                        "combination: both reconcile arithmetically and only one is real."
                    ),
                    evidence={"candidate_combinations": options},
                ),
                stats.name,
            )
            continue

        if not result.is_unique:
            continue

        chosen = [payable[i] for i in result.solutions[0]]
        settlement_ids = tuple(settlement.settlement_id for settlement in chosen)
        recomputed = sum(
            ctx.ledger.computed_net_paise(settlement_id) for settlement_id in settlement_ids
        )
        match = Match(
            kind=MatchKind.SETTLEMENT_BANK,
            left_id=txn.bank_txn_id,
            right_ids=settlement_ids,
            tier=Tier.AUTO,
            pass_name=stats.name,
            delta_paise=txn.amount_paise - recomputed,
            confidence=0.88,
            evidence={
                "sweep_size": len(settlement_ids),
                "components": [
                    {
                        "settlement_id": settlement.settlement_id,
                        "net_paise": ctx.ledger.computed_net_paise(settlement.settlement_id),
                    }
                    for settlement in chosen
                ],
                "credit_paise": txn.amount_paise,
                "nodes_expanded": result.nodes_expanded,
                "unique_solution": True,
            },
        )
        if ctx.accept(match, stats.name):
            stats.accepted += 1
            stats.bump(f"sweep_size_{len(settlement_ids)}")
        else:
            stats.rejected += 1

    stats.elapsed_ms = (time.perf_counter() - started) * 1000
    return stats


# ---------------------------------------------------------------------------
# Passes A and B: payment to order
# ---------------------------------------------------------------------------


def match_payment_order_reference(ctx: PassContext) -> PassStats:
    """Use the order reference the payment already carries.

    When present this is authoritative, so an amount difference is recorded as a
    finding against a confirmed pairing rather than as a reason to reject it.
    """
    stats = PassStats(name="A_payment_order_reference")
    started = time.perf_counter()

    for payment in sorted(ctx.ledger.payments.values(), key=lambda p: p.payment_id):
        if payment.order_id is None:
            continue
        if not ctx.assignments.payment_is_open(payment.payment_id):
            continue
        order = ctx.ledger.orders.get(payment.order_id)
        if order is None:
            # The payment names an order the merchant ledger does not contain.
            stats.bump("order_reference_dangling")
            continue

        stats.considered += 1
        delta = payment.gross_paise - order.amount_paise
        match = Match(
            kind=MatchKind.PAYMENT_ORDER,
            left_id=payment.payment_id,
            right_ids=(order.order_id,),
            tier=Tier.AUTO,
            pass_name=stats.name,
            delta_paise=delta,
            confidence=1.0,
            evidence={
                "order_reference": order.order_id,
                "payment_gross_paise": payment.gross_paise,
                "order_amount_paise": order.amount_paise,
            },
        )
        if not ctx.accept(match, stats.name):
            stats.rejected += 1
            continue

        stats.accepted += 1
        if not ctx.tolerance.within_amount(delta):
            stats.bump("amount_drift_flagged")
            ctx.raise_exception(
                ReconException(
                    kind=MatchKind.PAYMENT_ORDER,
                    subject_id=payment.payment_id,
                    reason_code=ReasonCode.ORDER_AMOUNT_DRIFT,
                    reason=(
                        f"Paired with {order.order_id} on its order reference, but the "
                        f"payment is {rupees(payment.gross_paise)} against an order of "
                        f"{rupees(order.amount_paise)}, a difference of {rupees(abs(delta))}"
                    ),
                    amount_paise=abs(delta),
                    suggested_action=(
                        "Check for a discount or partial capture applied after the "
                        "order was written. The pairing is not in doubt; the amount is."
                    ),
                    needs_human=True,
                    evidence={"delta_paise": delta},
                ),
                stats.name,
            )

    stats.elapsed_ms = (time.perf_counter() - started) * 1000
    return stats


def match_payment_order_amount_window(ctx: PassContext) -> PassStats:
    """Payments with no order reference: exact amount, tight window, unique only.

    This pass is the riskiest in the ladder and it is included deliberately.
    Payments genuinely arrive without a usable order reference, and a real
    controller does try to place them. Measuring how often that attempt is wrong,
    against ground truth, is more useful than omitting the pass and reporting a
    cleaner number that hides the question.

    Causality and one-to-one consumption are enforced by the verifier, so a
    payment cannot be attached to an order created after it.
    """
    stats = PassStats(name="B_payment_order_amount_window")
    started = time.perf_counter()
    window_days = 2

    for payment in sorted(ctx.ledger.payments.values(), key=lambda p: p.payment_id):
        if payment.order_id is not None:
            continue
        if not ctx.assignments.payment_is_open(payment.payment_id):
            continue

        captured = payment.captured_at
        candidates = [
            order
            for order in ctx.ledger.orders.values()
            if ctx.assignments.order_is_open(order.order_id)
            and order.amount_paise == payment.gross_paise
            and order.currency == payment.currency
            and order.created_at <= captured
            and (captured - order.created_at).days <= window_days
        ]
        stats.considered += 1
        if not candidates:
            stats.bump("no_candidate")
            continue
        if len(candidates) > 1:
            stats.declined_ambiguous += 1
            stats.bump("multiple_candidates")
            continue

        order = candidates[0]

        if (
            order.status is not OrderStatus.PAID
            and not ctx.tolerance.match_payments_to_inactive_orders
        ):
            # The only candidate is an order the merchant considers abandoned or
            # cancelled. Either its status is stale or the amounts coincide, and
            # nothing here can distinguish those. Declining sends it to the
            # exception ledger, which is the cheaper error: a wrong match here
            # closes a break permanently.
            stats.declined_ambiguous += 1
            stats.bump(f"declined_inactive_order_{order.status.value}")
            continue

        match = Match(
            kind=MatchKind.PAYMENT_ORDER,
            left_id=payment.payment_id,
            right_ids=(order.order_id,),
            tier=Tier.AUTO,
            pass_name=stats.name,
            delta_paise=0,
            confidence=0.70,
            evidence={
                "matched_on": "exact_amount_and_window",
                "window_days": window_days,
                "order_created_at": order.created_at.isoformat(),
                "payment_captured_at": captured.isoformat(),
                "order_status": order.status.value,
                "sole_candidate": True,
            },
        )
        if ctx.accept(match, stats.name):
            stats.accepted += 1
            stats.bump(f"order_status_{order.status.value}")
        else:
            stats.rejected += 1

    stats.elapsed_ms = (time.perf_counter() - started) * 1000
    return stats


# ---------------------------------------------------------------------------
# Ladder
# ---------------------------------------------------------------------------

SETTLEMENT_BANK_PASSES = (
    audit_settlement_net_identity,
    suppress_duplicate_credits,
    match_reference_exact,
    match_reference_tolerance,
    match_amount_window_exact,
    match_amount_window_tolerance,
    match_sweep_subset_sum,
)

PAYMENT_ORDER_PASSES = (
    match_payment_order_reference,
    match_payment_order_amount_window,
)


def run_deterministic_passes(ctx: PassContext) -> list[PassStats]:
    """Run the full ladder in order and return per-pass statistics."""
    results: list[PassStats] = []
    for pass_fn in SETTLEMENT_BANK_PASSES + PAYMENT_ORDER_PASSES:
        stats = pass_fn(ctx)
        results.append(stats)
        ctx.audit.record(
            stats.name,
            Action.PASS_COMPLETED,
            stats.name,
            {
                "considered": stats.considered,
                "accepted": stats.accepted,
                "rejected": stats.rejected,
                "declined_ambiguous": stats.declined_ambiguous,
                "elapsed_ms": round(stats.elapsed_ms, 3),
                "notes": stats.notes,
            },
        )
    return results
