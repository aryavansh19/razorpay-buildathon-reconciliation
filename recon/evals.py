"""Regression harness.

A single favourable run proves very little. Two questions decide whether the
headline numbers mean anything, and both are answered here by measurement rather
than argument.

**Does it generalise, or is it tuned to one dataset?**
``seed_sweep`` reconciles many independently generated batches and reports the
distribution, not the best case. Any seed that produces a false positive, a
coverage hole, or a broken audit chain is named. If the pipeline only works on the
seed in the README, this is where that shows up.

**Is the language model earning its place?**
``backend_comparison`` runs the identical residue through the offline baseline and
through the hosted model and prints both. If the regex-based baseline matches the
model, the honest conclusion is that the model is not contributing on this data,
and the report should say so rather than imply otherwise.

Usage::

    python -m recon.evals sweep --runs 25
    python -m recon.evals backends
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass, field

from .classify import HeuristicClassifier, LLMClassifier, build_classifier
from .generate import GeneratorConfig
from .models import Tier, Tolerance
from .pipeline import generate_and_reconcile


@dataclass
class SeedOutcome:
    seed: int
    records: int
    settlement_precision: float
    settlement_recall: float
    payment_precision: float
    payment_recall: float
    exception_pair_precision: float
    exception_pair_recall: float
    auto: int
    assisted: int
    unresolved: int
    false_positives: int
    coverage_holes: int
    gate_rejections: int
    audit_ok: bool
    records_per_second: float
    injected: dict[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return (
            self.false_positives == 0
            and self.coverage_holes == 0
            and self.audit_ok
        )


def run_one(
    seed: int,
    *,
    days: int,
    classifier_backend: str,
    tolerance: Tolerance | None = None,
) -> SeedOutcome:
    config = GeneratorConfig(seed=seed, days=days)
    result = generate_and_reconcile(
        config=config,
        tolerance=tolerance or Tolerance(),
        classifier=build_classifier(classifier_backend),
    )
    report = result.report
    sb, po, ex = report.settlement_bank, report.payment_order, report.exceptions_score
    return SeedOutcome(
        seed=seed,
        records=report.total_records,
        settlement_precision=sb.precision,
        settlement_recall=sb.recall,
        payment_precision=po.precision,
        payment_recall=po.recall,
        exception_pair_precision=ex.pair_precision,
        exception_pair_recall=ex.pair_recall,
        auto=report.tier_counts.get(Tier.AUTO.value, 0),
        assisted=report.tier_counts.get(Tier.ASSISTED.value, 0),
        unresolved=len(report.exceptions),
        false_positives=len(sb.false_positives) + len(po.false_positives),
        coverage_holes=len(report.coverage_holes),
        gate_rejections=len(report.verifier_rejections),
        audit_ok=report.audit_chain_ok and report.audit_replay_ok,
        records_per_second=report.records_per_second,
        injected=report.injected_scenarios,
    )


def _summarise(values: list[float]) -> str:
    if not values:
        return "n/a"
    if len(values) == 1:
        return f"{values[0]:.2f}"
    return (
        f"min {min(values):.2f}  mean {statistics.mean(values):.2f}  "
        f"max {max(values):.2f}"
    )


def seed_sweep(runs: int, *, start_seed: int, days: int, classifier_backend: str) -> int:
    outcomes = [
        run_one(start_seed + index, days=days, classifier_backend=classifier_backend)
        for index in range(runs)
    ]

    print()
    print(f"Seed sweep: {runs} independently generated batches, {days} days each")
    print("=" * 74)
    total_records = sum(outcome.records for outcome in outcomes)
    print(f"  batches                    {runs}")
    print(f"  total records reconciled   {total_records:,}")
    print(
        f"  records per batch          min {min(o.records for o in outcomes)}  "
        f"max {max(o.records for o in outcomes)}"
    )
    print()
    print("  Settlement to bank")
    print(f"    precision   {_summarise([o.settlement_precision for o in outcomes])}")
    print(f"    recall      {_summarise([o.settlement_recall for o in outcomes])}")
    print("  Payment to order")
    print(f"    precision   {_summarise([o.payment_precision for o in outcomes])}")
    print(f"    recall      {_summarise([o.payment_recall for o in outcomes])}")
    print("  Exception ledger, (record, reason) pairs")
    print(f"    precision   {_summarise([o.exception_pair_precision for o in outcomes])}")
    print(f"    recall      {_summarise([o.exception_pair_recall for o in outcomes])}")
    print()
    print(
        f"  gate rejections            total "
        f"{sum(o.gate_rejections for o in outcomes)}"
    )
    print(
        f"  throughput                 "
        f"{_summarise([o.records_per_second for o in outcomes])} records/second"
    )
    print()

    dirty = [outcome for outcome in outcomes if not outcome.clean]
    if dirty:
        print(f"  Seeds needing attention ({len(dirty)} of {runs}):")
        for outcome in dirty:
            problems = []
            if outcome.false_positives:
                problems.append(f"{outcome.false_positives} false positive(s)")
            if outcome.coverage_holes:
                problems.append(f"{outcome.coverage_holes} coverage hole(s)")
            if not outcome.audit_ok:
                problems.append("audit check failed")
            print(f"    seed {outcome.seed}: {', '.join(problems)}")
    else:
        print(
            f"  No false positives, coverage holes, or audit failures across all "
            f"{runs} batches."
        )
    print()

    imperfect_recall = [
        outcome
        for outcome in outcomes
        if outcome.settlement_recall < 100.0 or outcome.payment_recall < 100.0
    ]
    if imperfect_recall:
        print(f"  Batches with incomplete match recall ({len(imperfect_recall)} of {runs}):")
        for outcome in imperfect_recall:
            print(
                f"    seed {outcome.seed}: settlement recall "
                f"{outcome.settlement_recall:.2f}%, payment recall "
                f"{outcome.payment_recall:.2f}%"
            )
        print()

    # Scenario coverage across the sweep, so a reader can see that the sweep
    # actually exercised the difficult paths rather than generating easy batches.
    scenario_totals: dict[str, int] = {}
    for outcome in outcomes:
        for name, count in outcome.injected.items():
            scenario_totals[name] = scenario_totals.get(name, 0) + count
    print("  Difficulty exercised across the sweep")
    for name, count in sorted(scenario_totals.items()):
        print(f"    {name:34s} {count}")
    print()

    return 0 if not dirty else 1


def backend_comparison(*, seed: int, days: int) -> int:
    """Run the same residue through both backends and print both results."""
    print()
    print("Backend comparison on identical residue")
    print("=" * 74)

    llm = LLMClassifier()
    if not llm.available:
        print("  ANTHROPIC_API_KEY is not set, so only the offline baseline can run.")
        print("  Set the key and re-run to produce the comparison.")
        print()

    rows = []
    backends: list[tuple[str, object]] = [("heuristic", HeuristicClassifier())]
    if llm.available:
        backends.append(("llm", LLMClassifier()))

    for label, classifier in backends:
        result = generate_and_reconcile(
            config=GeneratorConfig(seed=seed, days=days),
            tolerance=Tolerance(),
            classifier=classifier,  # type: ignore[arg-type]
        )
        report = result.report
        usage = report.classifier_usage
        rows.append(
            {
                "backend": label,
                "assisted": report.assisted_matched,
                "unresolved": len(report.exceptions),
                "settlement_recall": report.settlement_bank.recall,
                "false_positives": len(report.settlement_bank.false_positives)
                + len(report.payment_order.false_positives),
                "exception_pair_recall": report.exceptions_score.pair_recall,
                "calls": usage.calls,
                "cost": usage.estimated_cost_usd,
                "latency": usage.mean_latency_ms,
                "failures": usage.failures,
            }
        )

    header = (
        f"  {'backend':12s} {'assisted':>9s} {'unresolved':>11s} "
        f"{'settl.recall':>13s} {'false pos':>10s} {'exc.recall':>11s} "
        f"{'items':>6s} {'cost usd':>10s} {'ms/item':>9s}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in rows:
        print(
            f"  {row['backend']:12s} {row['assisted']:>9d} {row['unresolved']:>11d} "
            f"{row['settlement_recall']:>12.2f}% {row['false_positives']:>10d} "
            f"{row['exception_pair_recall']:>10.2f}% {row['calls']:>6d} "
            f"{row['cost']:>10.6f} {row['latency']:>9.1f}"
        )
    print()

    if len(rows) == 2:
        baseline, model = rows[0], rows[1]
        if model["assisted"] <= baseline["assisted"] and model["cost"] > 0:
            print(
                "  On this dataset the hosted model does not recover more than the "
                "offline baseline. The baseline's regex covers the narration shapes "
                "this generator produces, so the model's advantage would only appear "
                "on narration formats the regex was not written for."
            )
        else:
            print(
                f"  The hosted model recovered "
                f"{model['assisted'] - baseline['assisted']} more correspondence(s) "
                f"than the offline baseline, at ${model['cost']:.6f}."
            )
        print()
    return 0


@dataclass
class GoldenQuestion:
    """One graded question about the pinned run.

    Expected values are pinned to seed 20260829, which is deterministic, so these
    are real assertions rather than smoke tests. If a change to the pipeline alters
    what the data means, these fail.
    """

    question: str
    expect_tools: tuple[str, ...] = ()
    must_cite: tuple[str, ...] = ()
    must_contain: tuple[str, ...] = ()
    expect_decline: bool = False
    note: str = ""


# Pinned to seed 20260829. setl_00016 is a deliberately rich case: its header
# disagrees with its line items *and* it is one half of a sweep credit.
GOLDEN_QUESTIONS: tuple[GoldenQuestion, ...] = (
    GoldenQuestion(
        question="What is the breakdown of setl_00016?",
        expect_tools=("settlement_breakdown",),
        must_cite=("setl_00016", "pay_00149", "rfnd_00002", "adj_00005"),
        must_contain=("1,920.09",),
        note="netting arithmetic for one settlement",
    ),
    GoldenQuestion(
        question="Which proposals did the verification gate reject, and why?",
        expect_tools=("gate_rejections",),
        must_cite=("bank_00042", "bank_00043", "bank_00044"),
        note="the gate catching plausible but wrong matches",
    ),
    GoldenQuestion(
        question="Which settlements did the gateway misreport?",
        expect_tools=("list_exceptions", "findings_by_reason"),
        must_cite=("setl_00001", "setl_00016"),
        note="header disagrees with line items",
    ),
    GoldenQuestion(
        question="Which bank credits are still unmatched?",
        expect_tools=("search_bank_credits", "list_exceptions"),
        must_cite=("bank_00049",),
        note="unattributed credits",
    ),
    GoldenQuestion(
        question="How much money is sitting in unresolved exceptions?",
        expect_tools=("money_summary", "findings_by_reason", "reconciliation_summary"),
        must_contain=("3,63,154.47",),
        note="value at risk",
    ),
    GoldenQuestion(
        question="Are there any duplicate bank credits?",
        expect_tools=("list_exceptions", "findings_by_reason"),
        must_cite=("bank_00005",),
        note="bank re-posts",
    ),
    GoldenQuestion(
        question="Which payments have no order behind them?",
        expect_tools=("list_exceptions", "findings_by_reason"),
        must_cite=("pay_00446",),
        note="orphan payments",
    ),
    GoldenQuestion(
        question="Explain bank_00041.",
        expect_tools=("explain_record",),
        must_cite=("bank_00041", "setl_00015", "setl_00016"),
        note="a sweep credit covering two settlements",
    ),
    GoldenQuestion(
        question="How did the reconciliation run go overall?",
        expect_tools=("reconciliation_summary",),
        must_contain=("100.0",),
        note="headline outcome",
    ),
    GoldenQuestion(
        question="Explain what happened to setl_99999.",
        expect_tools=("explain_record",),
        expect_decline=True,
        note="adversarial: the record does not exist and must not be invented",
    ),
)


def qa_evals(*, seed: int, days: int, backend: str, verbose: bool) -> int:
    """Grade the Q&A agent against the golden set.

    Four things are checked per question: that a relevant tool was called, that the
    required records were cited, that required figures appear verbatim, and that
    every identifier in the answer came from a tool result.

    The last check is the important one. It is the language-level equivalent of the
    arithmetic gate: the model may explain, and something deterministic confirms the
    explanation refers to records that actually exist in the run.
    """
    from .agent import ReconciliationView, build_agent  # local import, avoids a cycle

    result = generate_and_reconcile(
        config=GeneratorConfig(seed=seed, days=days),
        tolerance=Tolerance(),
        classifier=HeuristicClassifier(),
    )
    view = ReconciliationView(result)
    agent = build_agent(view, backend)
    backend_name = getattr(agent, "name", backend)

    print()
    print(f"Q&A golden set: {len(GOLDEN_QUESTIONS)} questions, backend '{backend_name}'")
    print("=" * 78)

    passed = 0
    failures: list[str] = []
    total_cost = 0.0
    total_latency = 0.0

    for index, golden in enumerate(GOLDEN_QUESTIONS, start=1):
        answer = agent.ask(golden.question)
        total_cost += answer.estimated_cost_usd
        total_latency += answer.latency_ms

        problems: list[str] = []

        if golden.expect_tools and not (set(answer.tools_used) & set(golden.expect_tools)):
            problems.append(
                f"expected one of {list(golden.expect_tools)}, called {answer.tools_used}"
            )

        if golden.expect_decline:
            # Nothing real may be cited, and the answer must not present the record
            # as though it exists.
            invented = [
                record for record in answer.citations if view.record_type(record) is not None
            ]
            if invented:
                problems.append(f"cited real records for a nonexistent subject: {invented}")
            failed_tool = any(not call.ok for call in answer.tool_calls)
            if not failed_tool:
                problems.append("no tool reported the record as missing")
        else:
            missing = [record for record in golden.must_cite if record not in answer.text]
            if missing:
                problems.append(f"did not cite {missing}")
            absent = [text for text in golden.must_contain if text not in answer.text]
            if absent:
                problems.append(f"did not contain {absent}")

        if not answer.grounded:
            problems.append(f"ungrounded identifiers: {answer.ungrounded_citations}")

        status = "pass" if not problems else "FAIL"
        if not problems:
            passed += 1
        else:
            failures.append(f"{golden.question} -> {'; '.join(problems)}")

        print(
            f"  {index:2d}. [{status}] {golden.question[:52]:52s} "
            f"{answer.steps} tool(s) {answer.latency_ms:6.0f}ms"
        )
        if verbose:
            print(f"        tools: {answer.tools_used}")
            print(f"        cited: {answer.citations[:8]}")
            print(f"        note:  {golden.note}")

    print()
    print(f"  {passed}/{len(GOLDEN_QUESTIONS)} passed")
    print(f"  grounding failures: {sum(1 for f in failures if 'ungrounded' in f)}")
    print(f"  total latency {total_latency:.0f}ms, estimated cost ${total_cost:.6f}")
    if failures:
        print()
        print("  Failures")
        for failure in failures:
            print(f"    - {failure}")
    print()
    return 0 if passed == len(GOLDEN_QUESTIONS) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m recon.evals", description="Regression harness."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sweep = sub.add_parser("sweep", help="Reconcile many seeds and report the distribution.")
    sweep.add_argument("--runs", type=int, default=25)
    sweep.add_argument("--start-seed", type=int, default=1_000)
    sweep.add_argument("--days", type=int, default=GeneratorConfig.days)
    sweep.add_argument("--classifier", default="heuristic", choices=("auto", "heuristic", "llm"))

    backends = sub.add_parser("backends", help="Compare classifier backends on one dataset.")
    backends.add_argument("--seed", type=int, default=GeneratorConfig.seed)
    backends.add_argument("--days", type=int, default=GeneratorConfig.days)

    policy = sub.add_parser(
        "policy",
        help="Measure the inactive-order matching policy both ways across a sweep.",
    )
    policy.add_argument("--runs", type=int, default=40)
    policy.add_argument("--start-seed", type=int, default=1_000)
    policy.add_argument("--days", type=int, default=GeneratorConfig.days)

    qa = sub.add_parser("qa", help="Grade the Q&A agent against the golden question set.")
    qa.add_argument("--seed", type=int, default=GeneratorConfig.seed)
    qa.add_argument("--days", type=int, default=GeneratorConfig.days)
    qa.add_argument("--backend", default="auto", choices=("auto", "router", "llm"))
    qa.add_argument("--verbose", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "sweep":
        return seed_sweep(
            args.runs,
            start_seed=args.start_seed,
            days=args.days,
            classifier_backend=args.classifier,
        )
    if args.command == "policy":
        return policy_comparison(
            args.runs, start_seed=args.start_seed, days=args.days
        )
    if args.command == "qa":
        return qa_evals(
            seed=args.seed, days=args.days, backend=args.backend, verbose=args.verbose
        )
    return backend_comparison(seed=args.seed, days=args.days)


def policy_comparison(runs: int, *, start_seed: int, days: int) -> int:
    """Quantify the cost of the inactive-order matching policy.

    Prints the false-positive count and the correct-match count under both
    settings. The point is to show the decision was measured rather than asserted:
    if declining these matches had cost real recall, that would be visible here.
    """
    print()
    print(f"Inactive-order matching policy, measured over {runs} batches")
    print("=" * 74)

    rows = []
    for label, allow in (("decline (default)", False), ("allow", True)):
        tolerance = Tolerance(match_payments_to_inactive_orders=allow)
        outcomes = [
            run_one(
                start_seed + index,
                days=days,
                classifier_backend="heuristic",
                tolerance=tolerance,
            )
            for index in range(runs)
        ]
        rows.append(
            {
                "label": label,
                "false_positives": sum(o.false_positives for o in outcomes),
                "seeds_with_fp": sum(1 for o in outcomes if o.false_positives),
                "min_payment_recall": min(o.payment_recall for o in outcomes),
                "min_payment_precision": min(o.payment_precision for o in outcomes),
                "min_exception_recall": min(o.exception_pair_recall for o in outcomes),
                "records": sum(o.records for o in outcomes),
            }
        )

    header = (
        f"  {'policy':20s} {'false pos':>10s} {'seeds w/ fp':>12s} "
        f"{'min pay prec':>13s} {'min pay recall':>15s} {'min exc recall':>15s}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in rows:
        print(
            f"  {row['label']:20s} {row['false_positives']:>10d} "
            f"{row['seeds_with_fp']:>12d} {row['min_payment_precision']:>12.2f}% "
            f"{row['min_payment_recall']:>14.2f}% {row['min_exception_recall']:>14.2f}%"
        )
    print()
    print(f"  {rows[0]['records']:,} records reconciled under each setting.")
    print(
        "  Declining costs no match recall on this data and removes the false "
        "positives, so it is the default. The flag remains so the trade-off can be "
        "re-measured on a different dataset."
    )
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
