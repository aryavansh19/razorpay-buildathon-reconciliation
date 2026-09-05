"""Ask questions about a reconciliation run.

    python -m recon.ask "why is bank_00049 unmatched?"
    python -m recon.ask                     # interactive
    python -m recon.ask --show-tools "how much is stuck in exceptions?"

The run is regenerated from the seed before answering, so a question always refers
to a reproducible batch. The agent reads the reconciled result through read-only
tools and cannot alter it.
"""

from __future__ import annotations

import argparse
import sys

from .agent import ReconciliationView, build_agent
from .classify import HeuristicClassifier
from .generate import GeneratorConfig
from .models import Tolerance
from .pipeline import generate_and_reconcile


def _print_answer(answer, show_tools: bool) -> None:
    print()
    print(answer.text)
    print()

    if show_tools and answer.tool_calls:
        print("  tool calls")
        for index, call in enumerate(answer.tool_calls, start=1):
            status = "ok" if call.ok else f"error: {call.error}"
            args = ", ".join(f"{k}={v!r}" for k, v in (call.args or {}).items())
            print(f"    {index}. {call.tool}({args}) {call.latency_ms:.1f}ms {status}")
        print()

    grounding = (
        "all identifiers traced to tool output"
        if answer.grounded
        else f"UNGROUNDED: {answer.ungrounded_citations}"
    )
    print(
        f"  backend {answer.backend} | {answer.steps} tool call(s) | "
        f"{answer.latency_ms:.0f}ms | {grounding}"
    )
    if answer.input_tokens or answer.output_tokens:
        print(
            f"  tokens {answer.input_tokens:,} in / {answer.output_tokens:,} out | "
            f"${answer.estimated_cost_usd:.6f}"
        )
    if answer.citations:
        print(f"  cited {len(answer.citations)} record(s): {', '.join(answer.citations[:12])}")
    if answer.error:
        print(f"  note: {answer.error}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m recon.ask",
        description="Question answering over a completed reconciliation run.",
    )
    parser.add_argument("question", nargs="*", help="Question to ask. Omit for interactive mode.")
    parser.add_argument("--seed", type=int, default=GeneratorConfig.seed)
    parser.add_argument("--days", type=int, default=GeneratorConfig.days)
    parser.add_argument(
        "--backend",
        choices=("auto", "router", "llm"),
        default="auto",
        help="'auto' uses the hosted model when ANTHROPIC_API_KEY is set, else the router.",
    )
    parser.add_argument("--show-tools", action="store_true", help="Print each tool call.")
    args = parser.parse_args(argv)

    result = generate_and_reconcile(
        config=GeneratorConfig(seed=args.seed, days=args.days),
        tolerance=Tolerance(),
        classifier=HeuristicClassifier(),
    )
    view = ReconciliationView(result)
    agent = build_agent(view, args.backend)

    report = result.report
    print(
        f"Reconciliation run seed {report.seed}: {report.total_records} records, "
        f"{report.auto_matched} auto-matched, {report.assisted_matched} model-assisted, "
        f"{len(report.exceptions)} unresolved."
    )
    print(f"Agent backend: {getattr(agent, 'name', 'unknown')}. Tools are read-only.")

    if args.question:
        _print_answer(agent.ask(" ".join(args.question)), args.show_tools)
        return 0

    print("Ask a question, or 'exit'.")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not question:
            continue
        if question.lower() in ("exit", "quit"):
            return 0
        _print_answer(agent.ask(question), args.show_tools)


if __name__ == "__main__":
    sys.exit(main())
