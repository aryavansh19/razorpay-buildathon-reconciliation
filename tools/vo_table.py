"""Print the narration window table straight from the cue sheet.

The table in VOICEOVER.md was hand-copied once and then went stale when the cue origin
was recalibrated, which is exactly the kind of thing that puts narration on the wrong
screen. Generate it instead.

    python tools/vo_table.py            # markdown table for VOICEOVER.md
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe"


def timecode(seconds: float) -> str:
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cues", default="media/cues.json")
    args = parser.parse_args()

    cues = json.loads(Path(args.cues).read_text(encoding="utf-8"))
    terminal_len = float(subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", "media/terminal.mp4"],
        capture_output=True, text=True, check=True).stdout.strip())

    labels = {
        "00a": "intro card", "01a": "the problem",
        "02a": "reconcile, setup", "02b": "reconcile, payoff",
        "03a": "arithmetic, setup", "03b": "arithmetic, payoff",
        "04a": "gate, setup", "04b": "gate, payoff",
        "05a": "false positive, setup", "05b": "false positive, payoff",
        "06a": "generalise, setup", "06b": "generalise, payoff",
        "07a": "agent, setup", "07b": "agent, payoff",
        "08a": "outro card", "99": "browser section",
    }

    print("| id | clip | window in the finished cut | length | words at 145 wpm |")
    print("|---|---|---|---|---|")
    for cue in cues:
        offset = terminal_len if cue["phase"] == "browser" else 0.0
        start, end = cue["from"] + offset, cue["to"] + offset
        print(f"| `{cue['id']}` | {labels.get(cue['id'], cue['title'])} | "
              f"{timecode(start)} - {timecode(end)} | {cue['seconds']:.1f}s | "
              f"{cue['words']} |")

    total = sum(c["seconds"] for c in cues)
    print()
    print(f"Sixteen windows, {total:.0f}s of narration room in a "
          f"{timecode(273.04)} cut.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
