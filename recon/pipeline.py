"""Orchestration.

Order of operations, and why it is this order:

1. Deterministic ladder, strictest first. Anything explainable by arithmetic is
   explained before a model is consulted, so the model is never in a position to
   claim credit for work the arithmetic already did.
2. Residue triage. Only what survives step 1 is shown to a classifier, together
   with the *open* candidates it may choose from. The candidate list is built by
   this module, not by the model, so a proposal cannot name a record that is out of
   window or already consumed.
3. Re-verification. Every model proposal goes through the same
   ``Verifier.accept`` as every deterministic proposal. A rejected proposal becomes
   an exception carrying the reason it failed, not a silent drop.
4. Audit checks. The hash chain is verified and the event stream is replayed, and
   the replayed state is compared to live state. If the log cannot reproduce the
   outcome, the run says so in the report rather than passing quietly.
5. Scoring against ground truth.

Residue is processed credits first, then settlements, then payments. Resolving a
credit consumes settlements, so doing credits first means the settlement stage
sees a smaller and more accurate residue. Reversing it would present the model
with candidates that are about to disappear.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .audit import Action, AuditLog
from .classify import Classifier, HeuristicClassifier, Proposal, ResidueItem
from .generate import GeneratorConfig, generate
from .metrics import RunReport, score_run
from .models import (
    GroundTruth,
    Ledger,
    Match,
    MatchKind,
    ReasonCode,
    ReconException,
    Tier,
    Tolerance,
    rupees,
)
from .passes import PassContext, PassStats, run_deterministic_passes
from .verify import Assignments, Verifier

RESIDUE_ACTOR = "residue_classifier"
MAX_CANDIDATES = 12


@dataclass
class PipelineResult:
    report: RunReport
    ledger: Ledger
    truth: GroundTruth
    audit: AuditLog
    matches: list[Match] = field(default_factory=list)
    exceptions: list[ReconException] = field(default_factory=list)
    pass_stats: list[PassStats] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Residue item construction
# ---------------------------------------------------------------------------


def _credit_residue_items(ctx: PassContext) -> list[ResidueItem]:
    items: list[ResidueItem] = []
    for txn in ctx.open_credits():
        if txn.bank_txn_id in ctx.adjudicated:
            continue
        candidates = []
        for settlement in ctx.open_settlements_near(txn):
            net = ctx.ledger.computed_net_paise(settlement.settlement_id)
            candidates.append(
                {
                    "id": settlement.settlement_id,
                    "recomputed_net_paise": net,
                    "recomputed_net": rupees(net),
                    "settled_on": settlement.settled_at.date().isoformat(),
                    "kind": settlement.kind.value,
                    "gateway_reference": settlement.utr,
                    "amount_delta_paise": txn.amount_paise - net,
                }
            )
        candidates.sort(key=lambda c: (abs(c["amount_delta_paise"]), c["id"]))
        items.append(
            ResidueItem(
                kind=MatchKind.SETTLEMENT_BANK,
                subject_id=txn.bank_txn_id,
                amount_paise=txn.amount_paise,
                description=(
                    f"Bank credit of {rupees(txn.amount_paise)} value dated "
                    f"{txn.value_date} that no settlement explained deterministically."
                ),
                candidates=candidates[:MAX_CANDIDATES],
                context={
                    "subject_type": "bank_credit",
                    "narration": txn.narration,
                    "value_date": txn.value_date.isoformat(),
                    "parsed_reference": txn.utr_hint,
                    "amount": rupees(txn.amount_paise),
                },
            )
        )
    return items


def _settlement_residue_items(ctx: PassContext) -> list[ResidueItem]:
    items: list[ResidueItem] = []
    for settlement_id, settlement in sorted(ctx.ledger.settlements.items()):
        if not ctx.assignments.settlement_is_open(settlement_id):
            continue
        if settlement_id in ctx.adjudicated:
            continue
        net = ctx.ledger.computed_net_paise(settlement_id)
        lines = ctx.ledger.lines_for_settlement(settlement_id)
        candidates = [
            {
                "id": txn.bank_txn_id,
                "amount_paise": txn.amount_paise,
                "amount": rupees(txn.amount_paise),
                "value_date": txn.value_date.isoformat(),
                "narration": txn.narration,
                "amount_delta_paise": txn.amount_paise - net,
            }
            for txn in ctx.open_credits()
            if ctx.tolerance.within_lag(settlement.settled_at, txn.value_date)
        ]
        candidates.sort(key=lambda c: (abs(c["amount_delta_paise"]), c["id"]))
        items.append(
            ResidueItem(
                kind=MatchKind.SETTLEMENT_BANK,
                subject_id=settlement_id,
                amount_paise=net,
                description=(
                    f"Settlement with a recomputed net of {rupees(net)} settled "
                    f"{settlement.settled_at.date()} that no bank credit explained."
                ),
                candidates=candidates[:MAX_CANDIDATES],
                context={
                    "subject_type": "settlement",
                    "settled_on": settlement.settled_at.date().isoformat(),
                    "kind": settlement.kind.value,
                    "gateway_reference": settlement.utr,
                    "gateway_reported_net_paise": settlement.net_paise,
                    "recomputed_net_paise": net,
                    "line_counts": {
                        name: len(values) for name, values in lines.items()
                    },
                },
            )
        )
    return items


def _payment_residue_items(ctx: PassContext) -> list[ResidueItem]:
    items: list[ResidueItem] = []
    for payment_id, payment in sorted(ctx.ledger.payments.items()):
        if not ctx.assignments.payment_is_open(payment_id):
            continue
        if payment_id in ctx.adjudicated:
            continue
        candidates = [
            {
                "id": order.order_id,
                "amount_paise": order.amount_paise,
                "amount": rupees(order.amount_paise),
                "created_at": order.created_at.isoformat(),
                "status": order.status.value,
                "customer_id": order.customer_id,
                "amount_delta_paise": payment.gross_paise - order.amount_paise,
            }
            for order in ctx.ledger.orders.values()
            if ctx.assignments.order_is_open(order.order_id)
            and order.created_at <= payment.captured_at
            and (payment.captured_at - order.created_at).days <= 3
        ]
        candidates.sort(key=lambda c: (abs(c["amount_delta_paise"]), c["id"]))
        items.append(
            ResidueItem(
                kind=MatchKind.PAYMENT_ORDER,
                subject_id=payment_id,
                amount_paise=payment.gross_paise,
                description=(
                    f"Payment of {rupees(payment.gross_paise)} captured "
                    f"{payment.captured_at.date()} with no usable order reference."
                ),
                candidates=candidates[:MAX_CANDIDATES],
                context={
                    "subject_type": "payment",
                    "captured_at": payment.captured_at.isoformat(),
                    "method": payment.method,
                    "order_reference_on_payment": payment.order_id,
                },
            )
        )
    return items


# ---------------------------------------------------------------------------
# Residue stage
# ---------------------------------------------------------------------------


def _handle_proposal(ctx: PassContext, item: ResidueItem, proposal: Proposal) -> None:
    ctx.audit.record(
        RESIDUE_ACTOR,
        Action.CLASSIFIER_PROPOSED if proposal.action != "abstain" else Action.CLASSIFIER_ABSTAINED,
        item.subject_id,
        {
            "backend": proposal.backend,
            "action": proposal.action,
            "right_ids": list(proposal.right_ids),
            "reason_code": proposal.reason_code.value if proposal.reason_code else None,
            "confidence": proposal.confidence,
            "rationale": proposal.rationale,
            "latency_ms": round(proposal.latency_ms, 2),
            "input_tokens": proposal.input_tokens,
            "output_tokens": proposal.output_tokens,
            "candidates_offered": [c["id"] for c in item.candidates],
        },
    )

    if proposal.is_match:
        # The classifier may be looking at a credit (fan-out to settlements) or at
        # a settlement (which must be flipped into the credit-left convention
        # before the verifier sees it).
        if item.kind is MatchKind.SETTLEMENT_BANK:
            if item.context.get("subject_type") == "settlement":
                left_id, right_ids = proposal.right_ids[0], (item.subject_id,)
            else:
                left_id, right_ids = item.subject_id, proposal.right_ids
        else:
            left_id, right_ids = item.subject_id, proposal.right_ids

        match = Match(
            kind=item.kind,
            left_id=left_id,
            right_ids=tuple(right_ids),
            tier=Tier.ASSISTED,
            pass_name=RESIDUE_ACTOR,
            delta_paise=0,
            confidence=proposal.confidence,
            evidence={
                "backend": proposal.backend,
                "rationale": proposal.rationale,
                "candidates_offered": [c["id"] for c in item.candidates],
                "proposal_reverified": True,
            },
        )
        if ctx.accept(match, RESIDUE_ACTOR):
            return

        # The proposal was plausible and did not survive re-derivation. That is the
        # gate doing its job, and the record stays open with the reason recorded.
        failure = ctx.verifier.rejections[-1][1].failure if ctx.verifier.rejections else ""
        ctx.raise_exception(
            ReconException(
                kind=item.kind,
                subject_id=item.subject_id,
                reason_code=ReasonCode.VERIFICATION_REJECTED,
                reason=(
                    f"Classifier ({proposal.backend}) proposed "
                    f"{list(proposal.right_ids)} with confidence "
                    f"{proposal.confidence:.2f}, rejected by verification: {failure}"
                ),
                amount_paise=item.amount_paise,
                suggested_action=(
                    "Review manually. The proposed correspondence is arithmetically "
                    "inconsistent with the ledger."
                ),
                evidence={"rationale": proposal.rationale},
            ),
            RESIDUE_ACTOR,
        )
        return

    if proposal.action == "exception" and proposal.reason_code is not None:
        ctx.raise_exception(
            ReconException(
                kind=item.kind,
                subject_id=item.subject_id,
                reason_code=proposal.reason_code,
                reason=proposal.rationale or "Classified as an exception.",
                amount_paise=item.amount_paise,
                suggested_action=proposal.suggested_action,
                needs_human=True,
                evidence={
                    "backend": proposal.backend,
                    "confidence": proposal.confidence,
                },
            ),
            RESIDUE_ACTOR,
        )
        return

    ctx.raise_exception(
        ReconException(
            kind=item.kind,
            subject_id=item.subject_id,
            reason_code=ReasonCode.CLASSIFIER_ABSTAINED,
            reason=(
                proposal.rationale
                or "Classifier declined to propose a correspondence for this record."
            ),
            amount_paise=item.amount_paise,
            suggested_action="Manual review required.",
            evidence={"backend": proposal.backend},
        ),
        RESIDUE_ACTOR,
    )


def _run_residue_stage(ctx: PassContext, classifier: Classifier) -> None:
    for build in (_credit_residue_items, _settlement_residue_items, _payment_residue_items):
        # Rebuilt between stages on purpose: the previous stage may have consumed
        # candidates, and offering a stale candidate list invites a proposal that
        # the verifier is guaranteed to reject.
        for item in build(ctx):
            proposal = classifier.classify(item)
            _handle_proposal(ctx, item, proposal)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def reconcile(
    ledger: Ledger,
    truth: GroundTruth,
    *,
    tolerance: Tolerance | None = None,
    classifier: Classifier | None = None,
    seed: int = 0,
) -> PipelineResult:
    tolerance = tolerance or Tolerance()
    classifier = classifier or HeuristicClassifier()
    audit = AuditLog()
    started = time.perf_counter()

    audit.record(
        "pipeline",
        Action.RUN_STARTED,
        "run",
        {
            "seed": seed,
            "records": ledger.record_count(),
            "classifier_backend": getattr(classifier, "name", "unknown"),
            "tolerance_amount_paise": tolerance.amount_paise,
            "tolerance_lag_days": tolerance.settlement_lag_days,
        },
    )

    ctx = PassContext(
        ledger=ledger,
        tolerance=tolerance,
        verifier=Verifier(ledger, tolerance),
        assignments=Assignments(),
        audit=audit,
    )

    pass_stats = run_deterministic_passes(ctx)
    _run_residue_stage(ctx, classifier)

    audit.record(
        "pipeline",
        Action.RUN_COMPLETED,
        "run",
        {"matches": len(ctx.matches), "exceptions": len(ctx.exceptions)},
    )
    wall_seconds = time.perf_counter() - started

    # -- audit self-checks -------------------------------------------------

    chain_ok, _ = AuditLog.verify_chain(audit.events)
    replayed = AuditLog.replay(audit.events)
    live_matches = {
        (match.kind.value, match.left_id, tuple(match.right_ids)) for match in ctx.matches
    }
    live_exceptions = {
        (exception.subject_id, exception.reason_code.value) for exception in ctx.exceptions
    }
    replay_ok = (
        replayed.accepted_matches == live_matches
        and replayed.raised_exceptions == live_exceptions
        and replayed.suppressed_credits == ctx.assignments.suppressed_bank
    )

    report = score_run(
        ledger=ledger,
        truth=truth,
        tolerance=tolerance,
        seed=seed,
        matches=ctx.matches,
        exceptions=ctx.exceptions,
        pass_stats=pass_stats,
        classifier_usage=classifier.usage,
        verifier_rejections=ctx.verifier.rejections,
        suppressed_bank=ctx.assignments.suppressed_bank,
        wall_seconds=wall_seconds,
        audit_events=len(audit),
        audit_chain_ok=chain_ok,
        audit_replay_ok=replay_ok,
    )

    return PipelineResult(
        report=report,
        ledger=ledger,
        truth=truth,
        audit=audit,
        matches=ctx.matches,
        exceptions=ctx.exceptions,
        pass_stats=pass_stats,
    )


def generate_and_reconcile(
    *,
    config: GeneratorConfig | None = None,
    tolerance: Tolerance | None = None,
    classifier: Classifier | None = None,
) -> PipelineResult:
    config = config or GeneratorConfig()
    ledger, truth = generate(config)
    return reconcile(
        ledger,
        truth,
        tolerance=tolerance,
        classifier=classifier,
        seed=config.seed,
    )
