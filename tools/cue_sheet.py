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


def ink_curve(ffmpeg: str, video: Path) -> list[float]:
    command = [
        ffmpeg, "-v", "error", "-i", str(video),
        "-vf", f"fps={SAMPLE_FPS},scale={WIDTH}:{HEIGHT},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    raw = subprocess.run(command, capture_output=True, check=True).stdout
    count = len(raw) // FRAME_BYTES
    return [
        sum(raw[i * FRAME_BYTES : (i + 1) * FRAME_BYTES]) / FRAME_BYTES
        for i in range(count)
    ]


def screen_clears(ink: list[float], band: float = 0.15) -> list[float]:
    """Times at which the screen went blank, one per beat."""
    if not ink:
        return []
    floor, ceiling = min(ink), max(ink)
    threshold = floor + band * (ceiling - floor or 1.0)
    clears: list[float] = []
    run_start: int | None = None
    for index, value in enumerate(ink + [ceiling]):
        if value <= threshold:
            if run_start is None:
                run_start = index
        elif run_start is not None:
            moment = run_start / SAMPLE_FPS
            if not clears or moment - clears[-1] > 4.0:
                clears.append(moment)
            run_start = None
    return clears


def calibrate(ffmpeg: str, video: Path, marks: list[dict], duration: float) -> float:
    """Video time corresponding to the demo's time origin.

    Detecting the origin by looking for the first frame with content does not work. The
    title card is sparse text, so any threshold high enough to ignore the countdown is
    also high enough to skip the card and latch onto a later, denser screen. That failure
    is silent and it shifts every cue by the size of the mistake.

    Instead the estimate comes from arithmetic: the recording is the origin offset, plus
    the span the demo reported, plus a short tail after it exits. That is then refined by
    sliding it against the detected screen clears and taking the offset where the demo's
    own beat starts line up best, which corrects whatever the tail actually was.
    """
    span = max(mark["at"] for mark in marks)
    rough = max(0.0, duration - span - 2.3)

    beat_starts = [m["at"] for m in marks if m["mark"] == "beat_start"]
    clears = screen_clears(ink_curve(ffmpeg, video))
    if not clears or not beat_starts:
        return rough

    best, best_error = rough, float("inf")
    step = 0.05
    candidate = max(0.0, rough - 4.0)
    while candidate <= rough + 4.0:
        error = 0.0
        for at in beat_starts:
            target = candidate + at
            error += min(abs(target - clear) for clear in clears) ** 2
        if error < best_error:
            best, best_error = candidate, error
        candidate += step

    mean_error = (best_error / len(beat_starts)) ** 0.5
    print(f"  origin {best:.2f}s  (estimate {rough:.2f}s, "
          f"mean alignment error {mean_error:.2f}s over {len(beat_starts)} beats)")
    if mean_error > 1.5:
        print("  WARNING: beats do not line up well; check the footage and timeline match")
    return best


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
    ffprobe = find_ffmpeg().replace("ffmpeg", "ffprobe")
    marks = json.loads(timeline_path.read_text(encoding="utf-8"))
    duration = float(subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, text=True, check=True).stdout.strip())
    print(f"{video.name}: {duration:.2f}s")
    offset = calibrate(ffmpeg, video, marks, duration)

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
