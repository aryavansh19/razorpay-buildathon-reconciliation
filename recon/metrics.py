"""Scoring against ground truth, and the report.

Everything here is measured, not asserted. The generator built the data forwards
and recorded the correct answer, so each claim in the report is checked rather
than demonstrated on a favourable example.

Three deliberate choices about what gets reported.

**Three numbers, not one.** Auto-matched, model-assisted-and-verified, and
unresolved are reported separately. A single blended "match rate" cannot be
audited, because it hides whether the result came from arithmetic or from a
language model.

**Pair-level precision, with false positives named.** A wrong match is not a
smaller version of a missed match. It silently closes a break a human would have
investigated, so false positives are listed individually rather than summarised.

**The exception ledger is scored too.** Ground truth declares which records
*should* be raised. That turns the exception list from a place to put leftovers
into a measurable output with its own precision and recall, and it means claiming
"we surface the hard cases" is falsifiable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .classify import (
    USD_PER_MILLION_INPUT_TOKENS,
    USD_PER_MILLION_OUTPUT_TOKENS,
    ClassifierUsage,
)
from .models import (
    GroundTruth,
    Ledger,
    Match,
    MatchKind,
    ReconException,
    Tier,
    Tolerance,
    rupees,
)
from .passes import PassStats


def _pct(numerator: float, denominator: float) -> float:
    return (numerator / denominator * 100) if denominator else 0.0


@dataclass
class PairScore:
    """Precision and recall over (left, right) correspondences."""

    label: str
    expected: set[tuple[str, str]] = field(default_factory=set)
    produced: set[tuple[str, str]] = field(default_factory=set)

    @property
    def true_positives(self) -> set[tuple[str, str]]:
        return self.expected & self.produced

    @property
    def false_positives(self) -> set[tuple[str, str]]:
        return self.produced - self.expected

    @property
    def false_negatives(self) -> set[tuple[str, str]]:
        return self.expected - self.produced

    @property
    def precision(self) -> float:
        produced = len(self.produced)
        return _pct(len(self.true_positives), produced) if produced else 100.0

    @property
    def recall(self) -> float:
        return _pct(len(self.true_positives), len(self.expected))

    @property
    def f1(self) -> float:
        precision, recall = self.precision, self.recall
        return (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0


@dataclass
class ExceptionScore:
    """How well the exception ledger matched the exceptions that should exist.

    Scored over ``(subject, reason)`` pairs rather than subjects, because one
    record can carry several independent findings and a pipeline that reports two
    real problems should not be penalised for it.

    ``wrong_reason`` is tracked separately from ``spurious`` because the two mean
    different things operationally. Naming the right record with the wrong reason
    still puts a human in front of a genuine break; inventing a break wastes their
    time.
    """

    expected: set[tuple[str, str]] = field(default_factory=set)
    raised: set[tuple[str, str]] = field(default_factory=set)

    @property
    def exact(self) -> set[tuple[str, str]]:
        return self.expected & self.raised

    @property
    def missed(self) -> set[tuple[str, str]]:
        return self.expected - self.raised

    @property
    def spurious(self) -> set[tuple[str, str]]:
        """Raised pairs whose subject should not have been raised at all."""
        expected_subjects = {subject for subject, _ in self.expected}
        return {pair for pair in self.raised - self.expected if pair[0] not in expected_subjects}

    @property
    def wrong_reason(self) -> set[tuple[str, str]]:
        """Right record, a reason that was not among the expected ones."""
        expected_subjects = {subject for subject, _ in self.expected}
        return {pair for pair in self.raised - self.expected if pair[0] in expected_subjects}

    @property
    def raised_total(self) -> int:
        return len(self.raised)

    @property
    def expected_total(self) -> int:
        return len(self.expected)

    @property
    def pair_precision(self) -> float:
        return _pct(len(self.exact), len(self.raised)) if self.raised else 100.0

    @property
    def pair_recall(self) -> float:
        return _pct(len(self.exact), len(self.expected))

    @property
    def subject_precision(self) -> float:
        """Share of raised records that are genuinely breaks, reason aside."""
        raised_subjects = {subject for subject, _ in self.raised}
        expected_subjects = {subject for subject, _ in self.expected}
        return (
            _pct(len(raised_subjects & expected_subjects), len(raised_subjects))
            if raised_subjects
            else 100.0
        )

    @property
    def subject_recall(self) -> float:
        raised_subjects = {subject for subject, _ in self.raised}
        expected_subjects = {subject for subject, _ in self.expected}
        return _pct(len(raised_subjects & expected_subjects), len(expected_subjects))


@dataclass
class CoverageHole:
    """A record that is neither matched, nor suppressed, nor raised.

    Reported because silence is the one outcome a reconciliation pipeline is never
    allowed to produce. A record that simply vanished is worse than a wrong
    answer, since nothing downstream will ever ask about it.
    """

    record_type: str
    record_id: str
    amount_paise: int


@dataclass
class RunReport:
    seed: int
    tolerance: Tolerance
    ledger_summary: dict[str, int]
    pass_stats: list[PassStats]
    settlement_bank: PairScore
    payment_order: PairScore
    exceptions_score: ExceptionScore
    tier_counts: dict[str, int]
    tier_value_paise: dict[str, int]
    coverage_holes: list[CoverageHole]
    classifier_usage: ClassifierUsage
    verifier_rejections: list[tuple[str, str, str]]
    injected_scenarios: dict[str, int]
    exceptions: list[ReconException]
    wall_seconds: float
    audit_events: int
    audit_chain_ok: bool
    audit_replay_ok: bool
    money: dict[str, int] = field(default_factory=dict)
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    # -- headline figures --------------------------------------------------

    @property
    def total_records(self) -> int:
        return self.ledger_summary.get("total", 0)

    @property
    def records_per_second(self) -> float:
        return self.total_records / self.wall_seconds if self.wall_seconds else 0.0

    @property
    def auto_matched(self) -> int:
        return self.tier_counts.get(Tier.AUTO.value, 0)

    @property
    def assisted_matched(self) -> int:
        return self.tier_counts.get(Tier.ASSISTED.value, 0)

    @property
    def unresolved(self) -> int:
        return len(self.exceptions)

    @property
    def total_correspondences_expected(self) -> int:
        return len(self.settlement_bank.expected) + len(self.payment_order.expected)

    @property
    def combined_match_rate(self) -> float:
        """Auto plus assisted, as a share of the correspondences that exist.

        Reported alongside the split rather than instead of it.
        """
        found = len(self.settlement_bank.true_positives) + len(
            self.payment_order.true_positives
        )
        return _pct(found, self.total_correspondences_expected)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "seed": self.seed,
            "tolerance": {
                "amount_paise": self.tolerance.amount_paise,
                "settlement_lag_days": self.tolerance.settlement_lag_days,
                "max_sweep_size": self.tolerance.max_sweep_size,
                "subset_sum_node_budget": self.tolerance.subset_sum_node_budget,
            },
            "batch": self.ledger_summary,
            "throughput": {
                "wall_seconds": round(self.wall_seconds, 3),
                "records_per_second": round(self.records_per_second, 1),
            },
            "tiers": {
                "auto_deterministic": self.auto_matched,
                "llm_assisted_verified": self.assisted_matched,
                "unresolved_exceptions": self.unresolved,
                "value_paise": self.tier_value_paise,
            },
            "match_quality": {
                "settlement_to_bank": {
                    "expected": len(self.settlement_bank.expected),
                    "true_positives": len(self.settlement_bank.true_positives),
                    "false_positives": sorted(self.settlement_bank.false_positives),
                    "false_negatives": sorted(self.settlement_bank.false_negatives),
                    "precision_pct": round(self.settlement_bank.precision, 2),
                    "recall_pct": round(self.settlement_bank.recall, 2),
                },
                "payment_to_order": {
                    "expected": len(self.payment_order.expected),
                    "true_positives": len(self.payment_order.true_positives),
                    "false_positives": sorted(self.payment_order.false_positives),
                    "false_negatives": sorted(self.payment_order.false_negatives),
                    "precision_pct": round(self.payment_order.precision, 2),
                    "recall_pct": round(self.payment_order.recall, 2),
                },
                "combined_match_rate_pct": round(self.combined_match_rate, 2),
            },
            "exception_ledger": {
                "raised_pairs": self.exceptions_score.raised_total,
                "expected_pairs": self.exceptions_score.expected_total,
                "exact": sorted(self.exceptions_score.exact),
                "wrong_reason": sorted(self.exceptions_score.wrong_reason),
                "missed": sorted(self.exceptions_score.missed),
                "spurious": sorted(self.exceptions_score.spurious),
                "pair_precision_pct": round(self.exceptions_score.pair_precision, 2),
                "pair_recall_pct": round(self.exceptions_score.pair_recall, 2),
                "subject_precision_pct": round(self.exceptions_score.subject_precision, 2),
                "subject_recall_pct": round(self.exceptions_score.subject_recall, 2),
            },
            "coverage_holes": [
                {
                    "record_type": hole.record_type,
                    "record_id": hole.record_id,
                    "amount_paise": hole.amount_paise,
                }
                for hole in self.coverage_holes
            ],
            "classifier": {
                "backend": self.classifier_usage.backend,
                "calls": self.classifier_usage.calls,
                "input_tokens": self.classifier_usage.input_tokens,
                "output_tokens": self.classifier_usage.output_tokens,
                "mean_latency_ms": round(self.classifier_usage.mean_latency_ms, 2),
                "estimated_cost_usd": round(self.classifier_usage.estimated_cost_usd, 6),
                "failures": self.classifier_usage.failures,
                "fallbacks": self.classifier_usage.fallbacks,
            },
            "verifier_rejections": [
                {"pass": pass_name, "subject": subject, "failure": failure}
                for pass_name, subject, failure in self.verifier_rejections
            ],
            "passes": [
                {
                    "name": stats.name,
                    "considered": stats.considered,
                    "accepted": stats.accepted,
                    "rejected": stats.rejected,
                    "declined_ambiguous": stats.declined_ambiguous,
                    "elapsed_ms": round(stats.elapsed_ms, 3),
                    "notes": stats.notes,
                }
                for stats in self.pass_stats
            ],
            "injected_scenarios": self.injected_scenarios,
            "money_paise": self.money,
            "audit": {
                "events": self.audit_events,
                "hash_chain_intact": self.audit_chain_ok,
                "replay_reproduces_state": self.audit_replay_ok,
            },
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_run(
    *,
    ledger: Ledger,
    truth: GroundTruth,
    tolerance: Tolerance,
    seed: int,
    matches: list[Match],
    exceptions: list[ReconException],
    pass_stats: list[PassStats],
    classifier_usage: ClassifierUsage,
    verifier_rejections: list[tuple[Match, object]],
    suppressed_bank: set[str],
    wall_seconds: float,
    audit_events: int,
    audit_chain_ok: bool,
    audit_replay_ok: bool,
) -> RunReport:
    settlement_bank = PairScore(label="settlement_to_bank")
    settlement_bank.expected = {
        (settlement_id, bank_id)
        for settlement_id, bank_ids in truth.settlement_to_bank.items()
        for bank_id in bank_ids
    }
    payment_order = PairScore(label="payment_to_order")
    payment_order.expected = set(truth.payment_to_order.items())

    tier_counts: dict[str, int] = {}
    tier_value: dict[str, int] = {}

    for match in matches:
        tier_counts[match.tier.value] = tier_counts.get(match.tier.value, 0) + 1
        if match.kind is MatchKind.SETTLEMENT_BANK:
            for settlement_id in match.right_ids:
                settlement_bank.produced.add((settlement_id, match.left_id))
            value = ledger.bank_txns[match.left_id].amount_paise
        else:
            payment_order.produced.add((match.left_id, match.right_ids[0]))
            value = ledger.payments[match.left_id].gross_paise
        tier_value[match.tier.value] = tier_value.get(match.tier.value, 0) + value

    # -- exception ledger --------------------------------------------------

    score = ExceptionScore(
        expected=truth.expected_exception_pairs(),
        raised={
            (exception.subject_id, exception.reason_code.value) for exception in exceptions
        },
    )
    raised = {exception.subject_id for exception in exceptions}

    # -- coverage ----------------------------------------------------------

    matched_settlements = {sid for sid, _ in settlement_bank.produced}
    matched_payments = {pid for pid, _ in payment_order.produced}
    matched_credits = {match.left_id for match in matches if match.kind is MatchKind.SETTLEMENT_BANK}
    accounted = set(raised) | suppressed_bank

    holes: list[CoverageHole] = []
    for settlement_id, settlement in ledger.settlements.items():
        if settlement_id not in matched_settlements and settlement_id not in accounted:
            holes.append(
                CoverageHole("settlement", settlement_id, ledger.computed_net_paise(settlement_id))
            )
    for txn in ledger.credits():
        if txn.bank_txn_id not in matched_credits and txn.bank_txn_id not in accounted:
            holes.append(CoverageHole("bank_credit", txn.bank_txn_id, txn.amount_paise))
    for payment_id, payment in ledger.payments.items():
        if payment_id not in matched_payments and payment_id not in accounted:
            holes.append(CoverageHole("payment", payment_id, payment.gross_paise))

    # -- money -------------------------------------------------------------

    money = {
        "gross_collected": sum(p.gross_paise for p in ledger.payments.values()),
        "fees_and_tax": sum(p.fee_paise + p.tax_paise for p in ledger.payments.values()),
        "refunds": sum(r.amount_paise for r in ledger.refunds.values()),
        "chargebacks": sum(
            c.amount_paise + c.fee_paise for c in ledger.chargebacks.values()
        ),
        "settled_net_computed": sum(
            ledger.computed_net_paise(settlement_id) for settlement_id in ledger.settlements
        ),
        "bank_credits_total": sum(t.amount_paise for t in ledger.credits()),
        "value_in_exceptions": sum(e.amount_paise for e in exceptions),
    }

    rejections = [
        (match.pass_name, match.left_id, str(getattr(result, "failure", "")))
        for match, result in verifier_rejections
    ]

    return RunReport(
        seed=seed,
        tolerance=tolerance,
        ledger_summary=ledger.summary(),
        pass_stats=pass_stats,
        settlement_bank=settlement_bank,
        payment_order=payment_order,
        exceptions_score=score,
        tier_counts=tier_counts,
        tier_value_paise=tier_value,
        coverage_holes=holes,
        classifier_usage=classifier_usage,
        verifier_rejections=rejections,
        injected_scenarios=dict(sorted(truth.injected.items())),
        exceptions=exceptions,
        wall_seconds=wall_seconds,
        audit_events=audit_events,
        audit_chain_ok=audit_chain_ok,
        audit_replay_ok=audit_replay_ok,
        money=money,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        rows = [["-"] * len(headers)]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [
        "| " + " | ".join(header.ljust(widths[i]) for i, header in enumerate(headers)) + " |",
        "|" + "|".join("-" * (width + 2) for width in widths) + "|",
    ]
    for row in rows:
        lines.append(
            "| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + " |"
        )
    return "\n".join(lines)


def render_markdown(report: RunReport) -> str:
    """The reviewer-facing report. Numbers first, caveats included."""
    sb, po, ex = report.settlement_bank, report.payment_order, report.exceptions_score
    out: list[str] = []

    out.append("# Reconciliation run report")
    out.append("")
    out.append(
        f"Seed `{report.seed}` | {report.total_records} records | "
        f"{report.wall_seconds:.2f}s wall | "
        f"{report.records_per_second:.0f} records/second | "
        f"generated {report.generated_at}"
    )
    out.append("")
    out.append(
        "All data is synthetic and generated locally from the seed above. "
        "Re-running with the same seed reproduces every figure in this report."
    )
    out.append("")

    out.append("## The three numbers")
    out.append("")
    out.append(
        _table(
            ["Tier", "Matches", "Value", "How it was decided"],
            [
                [
                    "Auto, deterministic",
                    str(report.auto_matched),
                    rupees(report.tier_value_paise.get(Tier.AUTO.value, 0)),
                    "Reference, amount and window arithmetic only",
                ],
                [
                    "Model-assisted, verified",
                    str(report.assisted_matched),
                    rupees(report.tier_value_paise.get(Tier.ASSISTED.value, 0)),
                    "Model proposed, deterministic gate re-derived and accepted",
                ],
                [
                    "Unresolved, raised",
                    str(report.unresolved),
                    rupees(report.money.get("value_in_exceptions", 0)),
                    "Surfaced to the exception ledger with a reason code",
                ],
            ],
        )
    )
    out.append("")
    out.append(
        f"Combined match rate against ground truth: "
        f"**{report.combined_match_rate:.2f}%** of "
        f"{report.total_correspondences_expected} correspondences that actually exist."
    )
    out.append("")

    out.append("## Match quality, scored against ground truth")
    out.append("")
    out.append(
        _table(
            ["Correspondence", "Expected", "Correct", "False positive", "Missed", "Precision", "Recall"],
            [
                [
                    "Settlement to bank",
                    str(len(sb.expected)),
                    str(len(sb.true_positives)),
                    str(len(sb.false_positives)),
                    str(len(sb.false_negatives)),
                    f"{sb.precision:.2f}%",
                    f"{sb.recall:.2f}%",
                ],
                [
                    "Payment to order",
                    str(len(po.expected)),
                    str(len(po.true_positives)),
                    str(len(po.false_positives)),
                    str(len(po.false_negatives)),
                    f"{po.precision:.2f}%",
                    f"{po.recall:.2f}%",
                ],
            ],
        )
    )
    out.append("")

    if sb.false_positives or po.false_positives:
        out.append("### False positives, listed individually")
        out.append("")
        out.append(
            "A wrong match closes a break nobody will look at again, so these are "
            "named rather than counted."
        )
        out.append("")
        for left, right in sorted(sb.false_positives):
            out.append(f"- settlement `{left}` was attributed to credit `{right}`")
        for left, right in sorted(po.false_positives):
            out.append(f"- payment `{left}` was attributed to order `{right}`")
        out.append("")
    else:
        out.append("No false positives in either correspondence.")
        out.append("")

    out.append("## Pass ladder")
    out.append("")
    out.append(
        _table(
            ["Pass", "Considered", "Accepted", "Rejected by gate", "Declined ambiguous", "ms"],
            [
                [
                    stats.name,
                    str(stats.considered),
                    str(stats.accepted),
                    str(stats.rejected),
                    str(stats.declined_ambiguous),
                    f"{stats.elapsed_ms:.1f}",
                ]
                for stats in report.pass_stats
            ],
        )
    )
    out.append("")

    if report.verifier_rejections:
        out.append("### Proposals the verification gate rejected")
        out.append("")
        out.append(
            f"{len(report.verifier_rejections)} proposals looked right to the pass "
            "that produced them and did not survive re-derivation from the ledger. "
            "Each of these would have been a false positive."
        )
        out.append("")
        for pass_name, subject, failure in report.verifier_rejections:
            out.append(f"- `{subject}` from `{pass_name}`: {failure}")
        out.append("")

    out.append("## Exception ledger")
    out.append("")
    out.append(
        "Scored over (record, reason) pairs, because one record can carry more than "
        "one independent finding."
    )
    out.append("")
    out.append(
        _table(
            ["Measure", "Value"],
            [
                ["Findings raised", str(ex.raised_total)],
                ["Findings that should exist", str(ex.expected_total)],
                ["Right record, right reason", str(len(ex.exact))],
                ["Right record, wrong reason", str(len(ex.wrong_reason))],
                ["Missed entirely", str(len(ex.missed))],
                ["Raised without cause", str(len(ex.spurious))],
                ["Pair precision", f"{ex.pair_precision:.2f}%"],
                ["Pair recall", f"{ex.pair_recall:.2f}%"],
                ["Record precision", f"{ex.subject_precision:.2f}%"],
                ["Record recall", f"{ex.subject_recall:.2f}%"],
            ],
        )
    )
    out.append("")

    if ex.missed:
        out.append("### Findings that should have been raised and were not")
        out.append("")
        for subject, reason in sorted(ex.missed):
            out.append(f"- `{subject}` expected `{reason}`")
        out.append("")
    if ex.wrong_reason:
        out.append("### Right record, wrong reason")
        out.append("")
        for subject, reason in sorted(ex.wrong_reason):
            expected = sorted(
                other_reason for other_subject, other_reason in ex.expected if other_subject == subject
            )
            out.append(f"- `{subject}`: raised `{reason}`, expected one of {expected}")
        out.append("")
    if ex.spurious:
        out.append("### Raised without cause")
        out.append("")
        for subject, reason in sorted(ex.spurious):
            out.append(f"- `{subject}` raised `{reason}`")
        out.append("")

    out.append("### Every unresolved item")
    out.append("")
    out.append(
        _table(
            ["Subject", "Reason", "Amount", "Needs human", "Detail"],
            [
                [
                    f"`{exception.subject_id}`",
                    exception.reason_code.value,
                    rupees(exception.amount_paise),
                    "yes" if exception.needs_human else "no",
                    exception.reason[:110],
                ]
                for exception in sorted(
                    report.exceptions, key=lambda e: (e.reason_code.value, e.subject_id)
                )
            ],
        )
    )
    out.append("")

    if report.coverage_holes:
        out.append("## Coverage holes")
        out.append("")
        out.append(
            "Records that were neither matched, suppressed, nor raised. These are "
            "reported because a record that disappears silently is the one outcome "
            "this pipeline must never produce."
        )
        out.append("")
        for hole in report.coverage_holes:
            out.append(
                f"- {hole.record_type} `{hole.record_id}` "
                f"({rupees(hole.amount_paise)})"
            )
        out.append("")
    else:
        out.append(
            "## Coverage\n\nEvery settlement, bank credit and payment is either "
            "matched, suppressed as a duplicate, or raised with a reason. No record "
            "was silently dropped.\n"
        )

    out.append("## Cost and latency")
    out.append("")
    usage = report.classifier_usage
    out.append(
        _table(
            ["Measure", "Value"],
            [
                ["Classifier backend", usage.backend or "none"],
                ["Residue items classified", str(usage.calls)],
                ["Input tokens", f"{usage.input_tokens:,}"],
                ["Output tokens", f"{usage.output_tokens:,}"],
                ["Mean latency per item", f"{usage.mean_latency_ms:.2f} ms"],
                ["Estimated cost", f"${usage.estimated_cost_usd:.6f}"],
                ["Model call failures", str(usage.failures)],
                ["Fell back to offline baseline", str(usage.fallbacks)],
                [
                    "Cost per 1,000 records",
                    f"${usage.estimated_cost_usd / max(report.total_records, 1) * 1000:.6f}",
                ],
            ],
        )
    )
    out.append("")
    out.append(
        "Token prices are declared configuration, not measurements: "
        f"${USD_PER_MILLION_INPUT_TOKENS:.2f} per million input tokens and "
        f"${USD_PER_MILLION_OUTPUT_TOKENS:.2f} per million output tokens. "
        "The cost figure is those rates multiplied by the token counts the API "
        "reported, so it can be recomputed or corrected."
    )
    out.append("")

    out.append("## Declared tolerances")
    out.append("")
    out.append(
        _table(
            ["Setting", "Value", "Why"],
            [
                [
                    "Amount tolerance",
                    f"{report.tolerance.amount_paise} paise",
                    "Absorbs sub-rupee bank rounding and nothing larger",
                ],
                [
                    "Settlement lag window",
                    f"{report.tolerance.settlement_lag_days} days",
                    "T+2 plus one day for holidays",
                ],
                [
                    "Max sweep size",
                    str(report.tolerance.max_sweep_size),
                    "Real sweeps collapse a few cycles, not many",
                ],
                [
                    "Subset-sum node budget",
                    f"{report.tolerance.subset_sum_node_budget:,}",
                    "Bounds an NP-complete search so it terminates",
                ],
            ],
        )
    )
    out.append("")
    out.append(
        "A wider tolerance would raise the match rate and lower precision. These "
        "values are printed so that trade-off is visible rather than buried."
    )
    out.append("")

    out.append("## Audit trail")
    out.append("")
    out.append(
        _table(
            ["Check", "Result"],
            [
                ["Events recorded", str(report.audit_events)],
                ["Hash chain intact", "pass" if report.audit_chain_ok else "FAIL"],
                [
                    "Replay reproduces final state",
                    "pass" if report.audit_replay_ok else "FAIL",
                ],
            ],
        )
    )
    out.append("")

    out.append("## Difficulty injected into this batch")
    out.append("")
    out.append(
        _table(
            ["Scenario", "Instances"],
            [[name, str(count)] for name, count in report.injected_scenarios.items()],
        )
    )
    out.append("")

    out.append("## Money")
    out.append("")
    out.append(
        _table(
            ["Line", "Amount"],
            [
                ["Gross collected", rupees(report.money["gross_collected"])],
                ["Fees and GST", rupees(report.money["fees_and_tax"])],
                ["Refunds", rupees(report.money["refunds"])],
                ["Chargebacks and dispute fees", rupees(report.money["chargebacks"])],
                ["Settled net, recomputed", rupees(report.money["settled_net_computed"])],
                ["Bank credits on statement", rupees(report.money["bank_credits_total"])],
                ["Value sitting in exceptions", rupees(report.money["value_in_exceptions"])],
            ],
        )
    )
    out.append("")

    return "\n".join(out)
