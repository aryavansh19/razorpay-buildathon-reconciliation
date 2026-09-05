"""Turn the demo's own timeline into a narration cue sheet.

The demo records exactly when each beat started, when its command began, when its output
finished, and when it went static. Those marks are relative to the moment the title card
printed, so the only thing that has to be recovered from the footage is where in the
video that moment is. One calibration point, found by looking for the first frame with
content on it, and everything else follows from the marks.

For each beat this prints two windows:

**setup**  from the beat heading appearing to the command starting. The screen is still.
           This is where the sentence explaining what is about to happen belongs.

**payoff** from the output finishing to the end of the beat. The screen is still again,
           showing the result. This is where the numbers belong.

Narration written to these windows cannot talk over scrolling output, which is the fault
that makes a walkthrough feel out of sync.

    python tools/cue_sheet.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

SAMPLE_FPS = 20
WIDTH, HEIGHT = 160, 90
FRAME_BYTES = WIDTH * HEIGHT
WORDS_PER_MINUTE = 145


def find_ffmpeg() -> str:
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    fallback = r"C:\ffmpeg\bin\ffmpeg.exe"
    if Path(fallback).exists():
        return fallback
    raise SystemExit("ffmpeg not found")


def find_first_content(ffmpeg: str, video: Path) -> float:
    """Video time at which the title card appears.

    Before this the console is blank or showing a countdown, so mean luma sits at its
    floor. The first sustained rise is the title card being printed, which is the demo's
    own time origin.
    """
    command = [
        ffmpeg, "-v", "error", "-i", str(video),
        "-vf", f"fps={SAMPLE_FPS},scale={WIDTH}:{HEIGHT},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    raw = subprocess.run(command, capture_output=True, check=True).stdout
    count = len(raw) // FRAME_BYTES
    ink = [
        sum(raw[i * FRAME_BYTES : (i + 1) * FRAME_BYTES]) / FRAME_BYTES
        for i in range(count)
    ]
    floor = min(ink)
    ceiling = max(ink)
    threshold = floor + 0.25 * (ceiling - floor)
    for index, value in enumerate(ink):
        if value >= threshold:
            return index / SAMPLE_FPS
    return 0.0


def words(seconds: float) -> int:
    return int(seconds / 60 * WORDS_PER_MINUTE)


def timecode(seconds: float) -> str:
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", default="media/terminal.mp4")
    parser.add_argument("--timeline", default="media/terminal.timeline.json")
    parser.add_argument("--browser", default="media/browser.mp4")
    parser.add_argument("--out", default="media/cues.json")
    args = parser.parse_args(argv)

    video, timeline_path = Path(args.video), Path(args.timeline)
    for path in (video, timeline_path):
        if not path.exists():
            raise SystemExit(f"{path} not found")

    ffmpeg = find_ffmpeg()
    marks = json.loads(timeline_path.read_text(encoding="utf-8"))
    offset = find_first_content(ffmpeg, video)
    print(f"{video.name}: title card appears at {offset:.2f}s, used as the origin")

    # Grouped by the order beats actually ran, not by their number. The intro and outro
    # cards both carry number 0, so keying on the number silently merges them and the
    # outro overwrites the intro.
    groups: list[dict] = []
    for mark in marks:
        if mark["mark"] == "beat_start":
            groups.append({"title": mark.get("title", "untitled"), "phases": {}})
        if not groups or mark.get("beat") is None:
            continue
        groups[-1]["phases"][mark["mark"]] = offset + mark["at"]

    cues: list[dict] = []
    print()
    print(f"  {'cue':26s} {'window':>15s} {'secs':>6s} {'words':>6s}")
    print("  " + "-" * 58)

    for position, group in enumerate(groups):
        phases = group["phases"]
        title = group["title"]
        start = phases.get("beat_start")
        if start is None:
            continue
        end = phases.get("beat_end", start)

        if "command_begin" in phases:
            setup_from, setup_to = start, phases["command_begin"]
            payoff_from = phases.get("output_end", phases["command_begin"])
            payoff_to = end
        else:
            # Text-only card: the whole beat is one still window.
            setup_from, setup_to = start, end
            payoff_from = payoff_to = None

        label = f"{position}. {title}"[:26]
        length = setup_to - setup_from
        cues.append({
            "id": f"{position:02d}a",
            "title": title,
            "phase": "setup" if payoff_from is not None else "card",
            "from": round(setup_from, 2),
            "to": round(setup_to, 2),
            "seconds": round(length, 1),
            "words": words(length),
        })
        print(f"  {label:26s} {timecode(setup_from):>7s}-{timecode(setup_to):<7s} "
              f"{length:6.1f} {words(length):6d}")

        if payoff_from is not None and payoff_to - payoff_from > 1.0:
            length = payoff_to - payoff_from
            cues.append({
                "id": f"{position:02d}b",
                "title": title,
                "phase": "payoff",
                "from": round(payoff_from, 2),
                "to": round(payoff_to, 2),
                "seconds": round(length, 1),
                "words": words(length),
            })
            print(f"  {'   ^ payoff':26s} {timecode(payoff_from):>7s}-"
                  f"{timecode(payoff_to):<7s} {length:6.1f} {words(length):6d}")

    browser = Path(args.browser)
    if browser.exists():
        duration = float(subprocess.run(
            [ffmpeg.replace("ffmpeg", "ffprobe"), "-v", "error",
             "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
             str(browser)], capture_output=True, text=True).stdout.strip() or 0)
        cues.append({
            "id": "99", "title": "browser section", "phase": "browser",
            "from": 0.0, "to": round(duration, 2),
            "seconds": round(duration, 1), "words": words(duration),
        })
        print()
        print(f"  {'browser section':26s} {'0:00.0':>7s}-{timecode(duration):<7s} "
              f"{duration:6.1f} {words(duration):6d}")

    Path(args.out).write_text(json.dumps(cues, indent=2), encoding="utf-8", newline="\n")
    total_words = sum(c["words"] for c in cues)
    total_secs = sum(c["seconds"] for c in cues)
    print()
    print(f"  {len(cues)} cues, {total_secs:.0f}s of narration room, "
          f"about {total_words} words at {WORDS_PER_MINUTE} wpm")
    print(f"  written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
