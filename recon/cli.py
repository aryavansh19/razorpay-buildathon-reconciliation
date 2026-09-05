"""Command line entry point.

    python -m recon.cli

Generates a synthetic batch, reconciles it, scores the result against ground
truth, and writes the dataset, the report, the exception ledger and the audit log.

Exit status is meaningful, so this is usable as a CI gate:

``0``
    The run completed and the pipeline behaved correctly. Note that this does not
    mean every record matched. Unresolved records are an expected and reported
    outcome, not a failure.
``1``
    The pipeline itself is defective: a record was neither matched, suppressed nor
    raised, or the audit log cannot reproduce its own outcome. Both are bugs
    rather than data problems.
``2``
    ``--strict`` was requested and the run produced at least one false positive.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .classify import build_classifier
from .generate import GeneratorConfig
from .html_report import render_html
from .metrics import render_markdown
from .models import Tolerance, rupees
from .pipeline import generate_and_reconcile
from .store import write_dataset, write_exception_ledger, write_ground_truth


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m recon.cli",
        description="Three-way payment reconciliation with measured output.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=GeneratorConfig.seed,
        help="Generator seed. The same seed reproduces every figure exactly.",
    )
    parser.add_argument(
        "--days", type=int, default=GeneratorConfig.days, help="Days of activity to generate."
    )
    parser.add_argument(
        "--classifier",
        choices=("auto", "heuristic", "llm"),
        default="auto",
        help=(
            "Residue backend. 'auto' uses the hosted model when ANTHROPIC_API_KEY is "
            "set and the offline baseline otherwise, so the run never depends on "
            "credentials."
        ),
    )
    parser.add_argument(
        "--amount-tolerance-paise",
        type=int,
        default=Tolerance.amount_paise,
        help="Amount tolerance in paise. Widening this raises match rate and lowers precision.",
    )
    parser.add_argument(
        "--lag-days",
        type=int,
        default=Tolerance.settlement_lag_days,
        help="Settlement to bank value date window, in days.",
    )
    parser.add_argument("--data-dir", default="data", help="Where to write the source CSVs.")
    parser.add_argument("--report-dir", default="reports", help="Where to write run artefacts.")
    parser.add_argument(
        "--publish",
        action="store_true",
        help=(
            "Also write docs/index.html, which is committed and served by GitHub Pages "
            "so the report can be read without cloning or running anything."
        ),
    )
    parser.add_argument(
        "--allow-inactive-order-match",
        action="store_true",
        help=(
            "Permit auto-matching a payment with no order reference to an order the "
            "merchant marked abandoned or cancelled. Off by default: a measured sweep "
            "showed this produces false positives and recovers no correct matches."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 if the run produces any false positive.",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress the stdout summary.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    config = GeneratorConfig(seed=args.seed, days=args.days)
    tolerance = Tolerance(
        amount_paise=args.amount_tolerance_paise,
        settlement_lag_days=args.lag_days,
        match_payments_to_inactive_orders=args.allow_inactive_order_match,
    )
    classifier = build_classifier(args.classifier)

    result = generate_and_reconcile(
        config=config, tolerance=tolerance, classifier=classifier
    )
    report = result.report

    data_dir, report_dir = Path(args.data_dir), Path(args.report_dir)
    write_dataset(result.ledger, data_dir)
    write_ground_truth(result.truth, data_dir / "ground_truth.json")
    markdown_path = report_dir / "report.md"
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    # Explicit LF endings so the artefacts diff cleanly regardless of the platform
    # they were generated on.
    markdown_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    (report_dir / "report.json").write_text(
        report.to_json(), encoding="utf-8", newline="\n"
    )
    # Self-contained HTML, written with the question box disabled because a static
    # file has no server behind it. `python -m recon.serve` serves a live copy.
    (report_dir / "report.html").write_text(
        render_html(result, live=False), encoding="utf-8", newline="\n"
    )
    write_exception_ledger(result.exceptions, report_dir / "exception_ledger.csv")
    result.audit.write_jsonl(report_dir / "audit.jsonl")

    published: Path | None = None
    if args.publish:
        published = Path("docs") / "index.html"
        published.parent.mkdir(parents=True, exist_ok=True)
        published.write_text(
            render_html(result, live=False), encoding="utf-8", newline="\n"
        )

    if not args.quiet:
        _print_summary(result, data_dir, report_dir, published)

    if report.coverage_holes or not report.audit_chain_ok or not report.audit_replay_ok:
        return 1
    if args.strict and (
        report.settlement_bank.false_positives or report.payment_order.false_positives
    ):
        return 2
    return 0


def _print_summary(
    result, data_dir: Path, report_dir: Path, published: Path | None = None
) -> None:
    report = result.report
    sb, po, ex = report.settlement_bank, report.payment_order, report.exceptions_score
    usage = report.classifier_usage

    print()
    print("Reconciliation run")
    print("=" * 62)
    print(f"  seed                  {report.seed}")
    print(f"  records               {report.total_records}")
    print(
        f"  throughput            {report.wall_seconds:.2f}s "
        f"({report.records_per_second:.0f} records/second)"
    )
    print(f"  classifier            {usage.backend or 'none'}")
    print()
    print("  Three numbers")
    print(f"    auto, deterministic    {report.auto_matched}")
    print(f"    model-assisted, verified {report.assisted_matched}")
    print(f"    unresolved, raised     {report.unresolved}")
    print()
    print("  Scored against ground truth")
    print(
        f"    settlement to bank   {len(sb.true_positives)}/{len(sb.expected)} correct, "
        f"precision {sb.precision:.2f}%, recall {sb.recall:.2f}%, "
        f"{len(sb.false_positives)} false positive(s)"
    )
    print(
        f"    payment to order     {len(po.true_positives)}/{len(po.expected)} correct, "
        f"precision {po.precision:.2f}%, recall {po.recall:.2f}%, "
        f"{len(po.false_positives)} false positive(s)"
    )
    print(f"    combined match rate  {report.combined_match_rate:.2f}%")
    print()
    print("  Exception ledger, scored over (record, reason) pairs")
    print(
        f"    raised {ex.raised_total}, should be {ex.expected_total}, "
        f"exact {len(ex.exact)}, wrong reason {len(ex.wrong_reason)}, "
        f"missed {len(ex.missed)}, without cause {len(ex.spurious)}"
    )
    print(
        f"    pair precision {ex.pair_precision:.2f}%, pair recall {ex.pair_recall:.2f}%, "
        f"record recall {ex.subject_recall:.2f}%"
    )
    print()
    print("  Integrity")
    print(f"    verification gate rejected  {len(report.verifier_rejections)} proposal(s)")
    print(f"    audit hash chain            {'intact' if report.audit_chain_ok else 'BROKEN'}")
    print(
        f"    audit replay                "
        f"{'reproduces state' if report.audit_replay_ok else 'MISMATCH'}"
    )
    print(f"    coverage holes              {len(report.coverage_holes)}")
    print()
    print("  Cost")
    print(
        f"    {usage.calls} residue item(s), "
        f"{usage.input_tokens:,} in / {usage.output_tokens:,} out tokens, "
        f"${usage.estimated_cost_usd:.6f}, "
        f"mean {usage.mean_latency_ms:.1f} ms/item"
    )
    if usage.failures:
        print(f"    model call failures         {usage.failures} (fell back to baseline)")
    print()
    print(f"  value in exceptions   {rupees(report.money['value_in_exceptions'])}")
    print()
    print(f"  data        {data_dir}/")
    print(f"  report      {report_dir}/report.md")
    print(f"  dashboard   {report_dir}/report.html   (open in a browser)")
    print(f"  json        {report_dir}/report.json")
    print(f"  exceptions  {report_dir}/exception_ledger.csv")
    print(f"  audit       {report_dir}/audit.jsonl")
    if published is not None:
        print(f"  published   {published}   (committed for GitHub Pages)")
    print()
    print("  For the interactive question box: python -m recon.serve")
    print()


if __name__ == "__main__":
    sys.exit(main())
