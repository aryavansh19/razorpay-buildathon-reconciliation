"""Paced terminal demo, for screen recording.

    python -m recon.demo              # full run, recording pace
    python -m recon.demo --fast       # rehearse without the waiting
    python -m recon.demo --beats 2,5  # only those beats, for a retake

The point of this module is a clean single take. Typing commands live during a
recording produces typos, dead air, and a cursor wandering around; a scripted
runner produces the same footage every time at a pace a viewer can actually read.
Each beat prints a title card, types its command out, runs the real thing, and
pauses long enough for the numbers to land.

Nothing is faked. Every command is executed as a subprocess against the real
pipeline and its actual output is streamed. If a number on screen is wrong, the
demo is wrong, which is the only property that makes a demo worth recording.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Narrow enough to stay readable when the terminal is scaled up for recording.
WIDTH = 74

ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
}

CLEAR_SCREEN = "\033[2J\033[3J\033[H"


def _unbuffer_stdout() -> None:
    """Force line buffering on our own stdout.

    This module prints narration and then hands the terminal to a subprocess that
    writes to the same stream. Python only line-buffers stdout when it is a tty, and
    when the demo is launched through a PowerShell pipeline it is not, so our prints
    sit in a block buffer while the child's output goes straight to the console.

    The visible symptom is narration appearing several beats late, under the wrong
    heading, which makes the recording look broken and is invisible when testing in
    an ordinary terminal.
    """
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001 - older streams, cosmetic only
        pass


def _supports_colour() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if sys.platform == "win32":
        # Windows 10+ consoles understand ANSI once virtual terminal processing is
        # enabled. Enabling it explicitly avoids escape codes printing as garbage
        # in the recording, which is unrecoverable in post.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            return True
        except Exception:  # noqa: BLE001 - cosmetic only, never fatal
            return False
    return sys.stdout.isatty()


COLOUR = _supports_colour()


def c(text: str, *styles: str) -> str:
    if not COLOUR:
        return text
    return "".join(ANSI[s] for s in styles) + text + ANSI["reset"]


@dataclass
class Beat:
    """One recorded moment: a title, some framing, and a command to run."""

    number: int
    title: str
    say: list[str] = field(default_factory=list)
    command: list[str] | None = None
    after: list[str] = field(default_factory=list)
    hold: float = 2.5
    # Seconds between revealed output lines, before the pace multiplier. Tuned per
    # beat: a dense table wants slower than a short summary.
    line_delay: float = 0.07
    # Seconds the heading and framing lines sit still *before* the command runs.
    #
    # This exists for the narration. Without it the setup sentence is still being
    # spoken while output is already scrolling past, so the viewer is being told what
    # is about to happen while it happens. Holding here lets the explanation land
    # first and the command run second, which is the order a person would do it in.
    setup_hold: float = 6.0


def _python() -> str:
    return sys.executable


BEATS: tuple[Beat, ...] = (
    Beat(
        number=0,
        title="Razorpay AI Buildathon, Track 04",
        say=[
            "AI Finance Controller.",
            "",
            "  Build an agent that closes one finance-ops loop across a 50+ record",
            "  batch of synthetic data, reporting its match rate and the exceptions",
            "  it could not resolve.",
            "",
            "  The bar: throughput, plus measured accuracy, plus an honest exception",
            "  list. One cherry-picked match proves nothing.",
            "",
            "",
            "What this is",
            "",
            "  Three-way reconciliation of a merchant's order ledger, a gateway",
            "  settlement report, and a bank statement.",
            "",
            "  1,165 records. Zero dependencies. One command.",
            "",
            "  Deterministic code proposes and disposes.",
            "  The model only ever proposes.",
        ],
        hold=14.0,
    ),
    Beat(
        number=1,
        title="The problem",
        say=[
            "A merchant has three sources of truth that never agree:",
            "  their order ledger, the gateway settlement report, the bank statement.",
            "",
            "One bank credit does not correspond to one payment. It corresponds to:",
            "",
            "  net = sum(payment.gross - fee - tax)",
            "      - sum(refund)",
            "      - sum(chargeback + dispute fee)",
            "      + sum(adjustment)",
            "",
            "Sweeps collapse several settlements into one line. Nets go negative and",
            "carry forward. References get truncated by the remitting bank. Banks",
            "re-post credits. This is still done by hand.",
        ],
        hold=6.0,
        line_delay=0.07,
    ),
    Beat(
        number=2,
        title="Reconcile a batch",
        say=["Deterministic arithmetic first. The model never sees this part."],
        command=[_python(), "-m", "recon.cli"],
        after=[
            "Three numbers, not one. A blended match rate cannot be audited,",
            "because it hides whether the answer came from arithmetic or a model.",
        ],
        hold=9.0,
        line_delay=0.08,
    ),
    Beat(
        number=3,
        title="Check the arithmetic by hand",
        say=[
            "setl_00016 is the interesting one: its header disagrees with its own",
            "line items, and it is half of a sweep credit.",
        ],
        command=[_python(), "-m", "recon.ask", "What is the breakdown of setl_00016?"],
        after=[
            "The money is right. The gateway's reported figure is wrong.",
            "A matching-only pipeline reports a perfect run and misses this entirely.",
        ],
        hold=9.0,
        line_delay=0.08,
    ),
    Beat(
        number=4,
        title="The verification gate, catching real mistakes",
        say=[
            "A sweep credit's narration echoes one member settlement's reference.",
            "A reference match looks correct and is wrong by lakhs.",
        ],
        command=[
            _python(),
            "-m",
            "recon.ask",
            "Which proposals did the verification gate reject?",
        ],
        after=[
            "Three proposals rejected. Each would have been a false positive.",
            "Subset-sum then resolved all three correctly.",
        ],
        hold=9.0,
        line_delay=0.09,
    ),
    Beat(
        number=5,
        title="A false positive I found, and fixed on cost",
        say=[
            "Matching orphan payments to abandoned orders by amount and window",
            "produced exactly one false positive in 46,191 records.",
            "Both readings were possible. The consequences were not symmetric.",
        ],
        command=[_python(), "-m", "recon.evals", "policy", "--runs", "40"],
        after=[
            "Declining costs zero correct matches and removes the false positive,",
            "so declining is the default. The flag survives so it can be re-measured.",
        ],
        hold=10.0,
        line_delay=0.25,
    ),
    Beat(
        number=6,
        title="Does it generalise, or is it tuned to one seed?",
        say=["200 independently generated batches. The distribution, not the best case."],
        command=[_python(), "-m", "recon.evals", "sweep", "--runs", "200"],
        after=[
            "230,638 records. Precision and recall at 100% min, mean and max.",
            "Zero false positives, zero coverage holes, zero audit failures.",
        ],
        hold=11.0,
        line_delay=0.08,
    ),
    Beat(
        number=7,
        title="The agent is graded too",
        say=[
            "Nine read-only tools over verified output. Every identifier in an answer",
            "is checked back against what the tools actually returned.",
            "One question is adversarial: it asks about a record that does not exist.",
        ],
        command=[_python(), "-m", "recon.evals", "qa"],
        after=["Groundedness is verified, not hoped for."],
        hold=8.0,
        line_delay=0.12,
    ),
    Beat(
        number=0,
        title="What is in the repo",
        say=[
            "  README.md          architecture, and the limitations stated plainly",
            "  reports/report.md  the full scored report",
            "  reports/report.html   browsable dashboard, drill into any record",
            "  reports/exception_ledger.csv   47 findings a human has to clear",
            "  reports/audit.jsonl   hash-chained, replayable decision log",
            "",
            "",
            "Reproduce every figure in this video:",
            "",
            "      python -m recon.cli",
            "",
            "  No dependencies. No API key. No network.",
            "",
            "",
            "  I wrote both the generator and the reconciler, so the test set only",
            "  contains difficulties I thought of. Every report lists which ones.",
        ],
        hold=16.0,
    ),
)


def _stream_command(command: list[str], pace: float, line_delay: float) -> int:
    """Run a command and reveal its output one line at a time.

    Letting the child write straight to the console dumps its entire output in a
    single frame. For anything longer than the window that means the terminal jumps
    to the tail and the headline numbers are never visible, and even when it does fit
    a wall of text appearing instantly is unreadable in a recording.

    Revealing line by line makes the terminal scroll at a pace a viewer can follow,
    the way it would if the work were genuinely taking that long. The child still
    runs at full speed; only the presentation is paced, and the delay collapses to
    nothing when ``pace`` is zero, so ``--fast`` stays fast.

    stderr is folded into stdout so interleaving is preserved. A child that writes to
    both would otherwise have its streams reordered relative to each other.
    """
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
    if process.stdout is not None:
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if pace and line_delay:
                time.sleep(line_delay * pace)
    process.wait()
    return process.returncode


def _type_out(text: str, pace: float) -> None:
    """Print a command as though it were typed, so the shot reads as live."""
    for char in text:
        sys.stdout.write(char)
        sys.stdout.flush()
        if pace:
            time.sleep(0.012 * pace)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _rule(char: str = "-") -> str:
    return char * WIDTH


def _wait(seconds: float, pace: float) -> None:
    if pace:
        time.sleep(seconds * pace)


# Numbered beats are the steps of the walkthrough. The intro and outro cards carry
# number 0 and are not labelled, so the count on screen matches what a viewer counts.
COMMAND_BEATS = sum(1 for b in BEATS if b.number)


# Marks emitted while the demo runs, in seconds from the moment the title card is
# printed. Detecting these from the footage afterwards is guesswork; the demo already
# knows them exactly, so it writes them down instead.
TIMELINE: list[dict] = []
_ORIGIN: float | None = None


def _mark(name: str, **fields) -> None:
    if _ORIGIN is None:
        return
    TIMELINE.append({"at": round(time.perf_counter() - _ORIGIN, 3), "mark": name, **fields})


def run_beat(beat: Beat, pace: float, total: int) -> int:
    # Each beat starts from a clean screen. Letting beats accumulate means a long
    # output like the seed sweep pushes its own headline numbers off the top before a
    # viewer can read them, and the top of the frame shows the tail of the previous
    # beat instead of this one's heading.
    if COLOUR:
        sys.stdout.write(CLEAR_SCREEN)
        sys.stdout.flush()
    print()
    print(c(_rule("="), "dim"))
    label = f"{beat.number}/{total}  " if beat.number else ""
    print(c(f"  {label}{beat.title}", "bold", "cyan"))
    print(c(_rule("="), "dim"))
    print()
    sys.stdout.flush()
    _mark('beat_start', beat=beat.number, title=beat.title)
    _wait(0.6, pace)

    for line in beat.say:
        print("  " + c(line, "dim") if line else "")
        sys.stdout.flush()
        _wait(0.5, pace)

    code = 0
    if beat.command:
        # Room for the setup narration before anything moves on screen.
        _mark('setup_begin', beat=beat.number)
        _wait(beat.setup_hold, pace)
        _mark('command_begin', beat=beat.number)
        print()
        display = " ".join(
            ["python" if part == sys.executable else part for part in beat.command]
        )
        # Quote the one argument that contains spaces, so the command shown is the
        # command a viewer could retype.
        if any(" " in part for part in beat.command[3:]):
            head = " ".join(
                ["python" if part == sys.executable else part for part in beat.command[:3]]
            )
            display = f'{head} "{" ".join(beat.command[3:])}"'
        sys.stdout.write(c("  $ ", "green"))
        _type_out(display, pace)
        print()
        sys.stdout.flush()
        _wait(0.4, pace)

        command = list(beat.command)
        if command and command[0] == sys.executable:
            command.insert(1, "-u")
        code = _stream_command(command, pace, beat.line_delay)
        sys.stdout.flush()
        _mark('output_end', beat=beat.number)
        _wait(2.0, pace)

    if beat.after:
        print()
        for line in beat.after:
            print("  " + c(line, "yellow") if line else "")
            sys.stdout.flush()
            _wait(0.5, pace)

    _mark('hold_begin', beat=beat.number)
    _wait(beat.hold, pace)
    _mark('beat_end', beat=beat.number)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m recon.demo",
        description="Paced terminal walkthrough, for screen recording.",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=1.0,
        help="Pace multiplier. 1.0 is recording speed, 0 removes all waiting.",
    )
    parser.add_argument("--fast", action="store_true", help="Same as --pace 0.")
    parser.add_argument(
        "--beats",
        default="",
        help="Comma separated beat numbers to run, for a retake. Default all.",
    )
    parser.add_argument(
        "--timeline",
        default="",
        help="Write exact beat timings to this JSON file, for narration alignment.",
    )
    parser.add_argument(
        "--countdown",
        type=int,
        default=0,
        help="Seconds to wait before starting, so you can start the recorder.",
    )
    args = parser.parse_args(argv)

    _unbuffer_stdout()
    pace = 0.0 if args.fast else max(0.0, args.pace)

    wanted: set[int] | None = None
    if args.beats.strip():
        try:
            wanted = {int(part) for part in args.beats.split(",") if part.strip()}
        except ValueError:
            print(f"--beats must be numbers, got {args.beats!r}", file=sys.stderr)
            return 2

    beats = [beat for beat in BEATS if wanted is None or beat.number in wanted]
    if not beats:
        print("No beats selected.", file=sys.stderr)
        return 2

    if args.countdown:
        for remaining in range(args.countdown, 0, -1):
            sys.stdout.write(f"\r  Starting in {remaining}...  ")
            sys.stdout.flush()
            time.sleep(1)
        sys.stdout.write("\r" + " " * 30 + "\r")

    global _ORIGIN
    _ORIGIN = time.perf_counter()
    _mark("origin")

    print()
    print(c(_rule("="), "blue"))
    print(c("  THREE-WAY SETTLEMENT RECONCILIATION", "bold"))
    print(c("  Razorpay AI Buildathon | Track 04, AI Finance Controller", "dim"))
    print(c(_rule("="), "blue"))
    print()
    print(
        "  "
        + c(
            "Deterministic code proposes and disposes. The model only ever proposes.",
            "bold",
        )
    )
    print("  " + c("All data is synthetic and generated locally from a seed.", "dim"))
    _wait(3.0, pace)

    failures: list[int] = []
    for beat in beats:
        if run_beat(beat, pace, COMMAND_BEATS) != 0:
            failures.append(beat.number)

    print()
    print(c(_rule("="), "blue"))
    print(c("  Repo, architecture and honest limitations in README.md", "bold"))
    print(c("  Dashboard: python -m recon.serve", "dim"))
    print(c(_rule("="), "blue"))
    print()

    _mark("finish")
    if args.timeline:
        path = Path(args.timeline)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(TIMELINE, indent=2), encoding="utf-8", newline="\n")
        print(f"  timeline written to {path} ({len(TIMELINE)} marks)")

    if failures:
        print(f"  Beats that exited non-zero: {failures}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
