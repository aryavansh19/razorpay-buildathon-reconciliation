"""The deterministic verification gate.

This module contains the only function in the pipeline permitted to turn a
candidate correspondence into an accepted match. Every proposal reaches it,
whether it came from an exact-reference lookup, a bounded subset-sum search, or a
language model reading a residue item.

That single-gate property is the architectural claim of the project. A model is
good at reading a garbled narration and guessing which settlement a credit
belongs to, and it is incapable of guaranteeing that the amounts add up, that the
records exist, or that neither side has already been consumed by another match.
So the model is allowed to *propose* and never to *decide*. Its proposal is
re-derived from the ledger here, and if the arithmetic does not close, the
proposal is rejected and becomes an exception no matter how confident the model
was.

Direction convention
--------------------
``MatchKind.SETTLEMENT_BANK``
    ``left_id`` is the bank transaction. ``right_ids`` are the settlements that
    credit explains. One credit can cover several settlements, which is what a
    sweep is, so the fan-out lives on the right.

``MatchKind.PAYMENT_ORDER``
    ``left_id`` is the payment, ``right_ids`` is the single order it belongs to.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Ledger, Match, MatchKind, OrderStatus, Tolerance


@dataclass
class Assignments:
    """Live one-to-one bookkeeping across all passes.

    Consumption tracking is not an optimisation. Without it, a later, looser pass
    happily re-matches a record an earlier, stricter pass already explained, and
    the resulting match count exceeds the number of records. A reconciliation
    report that double-counts is worse than no report.
    """

    # bank_txn_id -> the match that consumed it
    bank_to_match: dict[str, Match] = field(default_factory=dict)
    # settlement_id -> bank_txn_id that explains it
    settlement_to_bank: dict[str, str] = field(default_factory=dict)
    # payment_id -> order_id
    payment_to_order: dict[str, str] = field(default_factory=dict)
    # order_id -> payment_id, enforcing that one order absorbs one payment
    order_to_payment: dict[str, str] = field(default_factory=dict)
    # Bank credits removed from matching before it began, because they are
    # duplicate re-posts. Kept separate from unmatched so the report can tell a
    # suppressed record from an unexplained one.
    suppressed_bank: set[str] = field(default_factory=set)

    def settlement_is_open(self, settlement_id: str) -> bool:
        return settlement_id not in self.settlement_to_bank

    def bank_is_open(self, bank_txn_id: str) -> bool:
        return bank_txn_id not in self.bank_to_match and bank_txn_id not in self.suppressed_bank

    def payment_is_open(self, payment_id: str) -> bool:
        return payment_id not in self.payment_to_order

    def order_is_open(self, order_id: str) -> bool:
        return order_id not in self.order_to_payment

    def commit(self, match: Match) -> None:
        """Record an accepted match. Only ``Verifier.accept`` calls this."""
        if match.kind is MatchKind.SETTLEMENT_BANK:
            self.bank_to_match[match.left_id] = match
            for settlement_id in match.right_ids:
                self.settlement_to_bank[settlement_id] = match.left_id
        else:
            order_id = match.right_ids[0]
            self.payment_to_order[match.left_id] = order_id
            self.order_to_payment[order_id] = match.left_id


@dataclass
class VerificationResult:
    """Per-check outcome, retained for the audit trail.

    The individual checks are kept rather than collapsed to a boolean so that a
    rejection can be explained. "The model proposed this and verification said no"
    is not a useful audit entry; "the recomputed net differs by INR 812.40" is.
    """

    ok: bool
    checks: dict[str, bool] = field(default_factory=dict)
    failure: str | None = None
    recomputed_delta_paise: int = 0

    @classmethod
    def failed(cls, check: str, reason: str, checks: dict[str, bool], delta: int = 0):
        checks = dict(checks)
        checks[check] = False
        return cls(ok=False, checks=checks, failure=reason, recomputed_delta_paise=delta)


class Verifier:
    """Re-derives every proposed match from the ledger before accepting it."""

    def __init__(self, ledger: Ledger, tolerance: Tolerance) -> None:
        self.ledger = ledger
        self.tolerance = tolerance
        self.rejections: list[tuple[Match, VerificationResult]] = []

    # -- public API --------------------------------------------------------

    def accept(self, match: Match, assignments: Assignments) -> VerificationResult:
        """Verify and, on success, commit. The only path to an accepted match."""
        result = self.verify(match, assignments)
        if result.ok:
            match.delta_paise = result.recomputed_delta_paise
            assignments.commit(match)
        else:
            self.rejections.append((match, result))
        return result

    def verify(self, match: Match, assignments: Assignments) -> VerificationResult:
        if match.kind is MatchKind.SETTLEMENT_BANK:
            return self._verify_settlement_bank(match, assignments)
        return self._verify_payment_order(match, assignments)

    # -- settlement to bank ------------------------------------------------

    def _verify_settlement_bank(
        self, match: Match, assignments: Assignments
    ) -> VerificationResult:
        checks: dict[str, bool] = {}

        bank_txn = self.ledger.bank_txns.get(match.left_id)
        if bank_txn is None:
            return VerificationResult.failed(
                "ids_exist", f"bank transaction {match.left_id} does not exist", checks
            )
        if not match.right_ids:
            return VerificationResult.failed(
                "ids_exist", "match proposes no settlements", checks
            )
        missing = [sid for sid in match.right_ids if sid not in self.ledger.settlements]
        if missing:
            return VerificationResult.failed(
                "ids_exist", f"settlements do not exist: {missing}", checks
            )
        if len(set(match.right_ids)) != len(match.right_ids):
            return VerificationResult.failed(
                "ids_exist", "match repeats a settlement", checks
            )
        checks["ids_exist"] = True

        # A credit is money arriving. A debit can never explain a settlement, and
        # a pipeline that scans statement lines rather than credits will try.
        if not bank_txn.is_credit:
            return VerificationResult.failed(
                "credit_direction",
                f"{bank_txn.bank_txn_id} is a debit of {bank_txn.amount_paise} paise",
                checks,
            )
        checks["credit_direction"] = True

        if not assignments.bank_is_open(match.left_id):
            return VerificationResult.failed(
                "not_consumed", f"{match.left_id} is already matched or suppressed", checks
            )
        already = [sid for sid in match.right_ids if not assignments.settlement_is_open(sid)]
        if already:
            return VerificationResult.failed(
                "not_consumed", f"settlements already matched: {already}", checks
            )
        checks["not_consumed"] = True

        # The identity. Recomputed from line items, never read from the
        # settlement header, because the header is one of the things under test.
        recomputed = sum(self.ledger.computed_net_paise(sid) for sid in match.right_ids)
        delta = bank_txn.amount_paise - recomputed
        if not self.tolerance.within_amount(delta):
            return VerificationResult.failed(
                "amount_identity",
                (
                    f"recomputed net {recomputed} paise vs credit "
                    f"{bank_txn.amount_paise} paise, delta {delta} paise "
                    f"exceeds tolerance {self.tolerance.amount_paise}"
                ),
                checks,
                delta,
            )
        checks["amount_identity"] = True

        # Every settlement in the group must plausibly have been paid by this
        # credit. A sweep spanning three weeks is not a sweep, it is a coincidence
        # of arithmetic.
        for settlement_id in match.right_ids:
            settlement = self.ledger.settlements[settlement_id]
            if not self.tolerance.within_lag(settlement.settled_at, bank_txn.value_date):
                return VerificationResult.failed(
                    "date_window",
                    (
                        f"{settlement_id} settled {settlement.settled_at.date()} but "
                        f"credit value date is {bank_txn.value_date}, outside the "
                        f"{self.tolerance.settlement_lag_days} day window"
                    ),
                    checks,
                    delta,
                )
        checks["date_window"] = True

        return VerificationResult(ok=True, checks=checks, recomputed_delta_paise=delta)

    # -- payment to order --------------------------------------------------

    def _verify_payment_order(
        self, match: Match, assignments: Assignments
    ) -> VerificationResult:
        checks: dict[str, bool] = {}

        payment = self.ledger.payments.get(match.left_id)
        if payment is None:
            return VerificationResult.failed(
                "ids_exist", f"payment {match.left_id} does not exist", checks
            )
        if len(match.right_ids) != 1:
            return VerificationResult.failed(
                "ids_exist",
                f"a payment maps to exactly one order, got {len(match.right_ids)}",
                checks,
            )
        order = self.ledger.orders.get(match.right_ids[0])
        if order is None:
            return VerificationResult.failed(
                "ids_exist", f"order {match.right_ids[0]} does not exist", checks
            )
        checks["ids_exist"] = True

        if not assignments.payment_is_open(payment.payment_id):
            return VerificationResult.failed(
                "not_consumed", f"{payment.payment_id} is already matched", checks
            )
        if not assignments.order_is_open(order.order_id):
            return VerificationResult.failed(
                "not_consumed", f"{order.order_id} is already matched", checks
            )
        checks["not_consumed"] = True

        if payment.currency != order.currency:
            return VerificationResult.failed(
                "currency",
                f"payment is {payment.currency}, order is {order.currency}",
                checks,
            )
        checks["currency"] = True

        # A payment cannot precede the order that caused it. This catches a whole
        # class of plausible-looking amount-and-window matches that are causally
        # impossible, which is the main way the no-reference path goes wrong.
        if payment.captured_at < order.created_at:
            return VerificationResult.failed(
                "causality",
                (
                    f"payment captured {payment.captured_at.isoformat()} precedes "
                    f"order created {order.created_at.isoformat()}"
                ),
                checks,
            )
        checks["causality"] = True

        # The inactive-order policy is enforced here rather than only in the pass
        # that first considered it. A model reading the residue sees the same
        # candidates and would otherwise be able to reintroduce exactly the false
        # positive the policy exists to prevent. Putting it in the gate means every
        # path obeys it, which is the point of having a single gate.
        if (
            order.status is not OrderStatus.PAID
            and not self.tolerance.match_payments_to_inactive_orders
        ):
            return VerificationResult.failed(
                "order_active",
                (
                    f"{order.order_id} is {order.status.value} in the merchant ledger; "
                    "policy declines auto-matching a payment with no order reference "
                    "to an inactive order"
                ),
                checks,
            )
        checks["order_active"] = True

        # An amount difference is deliberately *not* a rejection here. When a
        # payment carries an order reference, the pairing is a fact and the
        # difference is a finding about that pairing, usually a discount applied
        # after the order was written or a partial capture. Rejecting the match
        # would discard the one thing we know for certain and leave the payment
        # looking unexplained. The pipeline records the match and raises the drift
        # separately, so the report shows both: the pairing was identified, and
        # the amounts disagree by this much.
        delta = payment.gross_paise - order.amount_paise
        return VerificationResult(
            ok=True, checks=checks, recomputed_delta_paise=delta
        )
