"""Find the fine-grained sync anchors inside each beat of the footage.

Aligning narration to beat boundaries alone is not enough. Within a beat the terminal
does three different things, and the narration has to track them:

1. the heading and its framing lines print, and stay still
2. the command types out and its output scrolls past
3. the output finishes and the screen goes static again

If narration is simply laid over the beat, the setup sentence is still being spoken
while output races by, and the payoff sentence with the numbers in it arrives after the
terminal has already moved on.

So this locates, per beat, the moment output *starts* and the moment it *stops*, by
measuring how much the frame changes from one sample to the next. A static terminal has
near-zero change; scrolling output has a lot. Those two timestamps are the anchors the
re-timing hangs off: freeze before output starts to let the setup line land, and freeze
after it stops to let the numbers land.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SAMPLE_FPS = 10
WIDTH, HEIGHT = 240, 135
FRAME_BYTES = WIDTH * HEIGHT

# Beat starts in media/walkthrough.mp4, confirmed by reading the heading from a frame.
BEAT_STARTS = [7.5, 28.5, 56.3, 85.5, 114.0, 148.2, 194.3]

# Mean absolute per-pixel change, on a 0-255 scale, above which the screen counts as
# actively changing. Chosen well above encoder noise on a static frame and well below a
# single line of text scrolling in.
ACTIVITY_THRESHOLD = 0.35


def find_tool(name: str, fallback: str) -> str:
    if shutil.which(name):
        return name
    if Path(fallback).exists():
        return fallback
    raise SystemExit(f"{name} not found")


def sample_activity(ffmpeg: str, video: Path) -> list[float]:
    """Mean absolute frame-to-frame difference per sample."""
    command = [
        ffmpeg, "-v", "error", "-i", str(video),
        "-vf", f"fps={SAMPLE_FPS},scale={WIDTH}:{HEIGHT},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    raw = subprocess.run(command, capture_output=True, check=True).stdout
    count = len(raw) // FRAME_BYTES
    frames = [raw[i * FRAME_BYTES : (i + 1) * FRAME_BYTES] for i in range(count)]
    activity = [0.0]
    for index in range(1, count):
        previous, current = frames[index - 1], frames[index]
        total = 0
        # Stride the pixels; full comparison is needlessly slow and the signal is
        # strong enough that a quarter of them is plenty.
        for offset in range(0, FRAME_BYTES, 4):
            total += abs(current[offset] - previous[offset])
        activity.append(total / (FRAME_BYTES / 4))
    return activity


def anchors_for_beat(
    activity: list[float], start: float, end: float
) -> tuple[float, float]:
    """Return (output_start, output_end) inside one beat."""
    first = int(start * SAMPLE_FPS)
    last = min(int(end * SAMPLE_FPS), len(activity) - 1)
    active = [i for i in range(first, last + 1) if activity[i] >= ACTIVITY_THRESHOLD]
    if not active:
        midpoint = (start + end) / 2
        return midpoint, midpoint

    # Output start: the beginning of the last sustained run of activity. Earlier
    # activity is the heading and framing lines printing, which is not what the
    # narration needs to wait for.
    runs: list[list[int]] = []
    for index in active:
        if runs and index - runs[-1][-1] <= 6:
            runs[-1].append(index)
        else:
            runs.append([index])
    longest = max(runs, key=len)
    return longest[0] / SAMPLE_FPS, longest[-1] / SAMPLE_FPS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", nargs="?", default="media/walkthrough.mp4")
    parser.add_argument("--json", default="media/anchors.json")
    args = parser.parse_args(argv)

    video = Path(args.video)
    if not video.exists():
        raise SystemExit(f"{video} not found")

    ffmpeg = find_tool("ffmpeg", r"C:\ffmpeg\bin\ffmpeg.exe")
    ffprobe = find_tool("ffprobe", r"C:\ffmpeg\bin\ffprobe.exe")
    duration = float(subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, text=True, check=True).stdout.strip())

    print(f"{video}  {duration:.2f}s")
    print("sampling frame-to-frame activity...")
    activity = sample_activity(ffmpeg, video)
    print(f"{len(activity)} samples, peak change {max(activity):.2f}")
    print()

    bounds = BEAT_STARTS + [duration]
    result = []
    print(f"  {'beat':4s} {'starts':>7s} {'out from':>9s} {'out to':>8s} "
          f"{'static tail':>12s} {'setup room':>11s}")
    print("  " + "-" * 60)
    for index, start in enumerate(BEAT_STARTS):
        end = bounds[index + 1]
        out_start, out_end = anchors_for_beat(activity, start, end)
        result.append({
            "beat": index + 1,
            "start": round(start, 2),
            "end": round(end, 2),
            "output_start": round(out_start, 2),
            "output_end": round(out_end, 2),
        })
        print(f"  {index + 1:<4d} {start:7.1f} {out_start:9.1f} {out_end:8.1f} "
              f"{end - out_end:11.1f}s {out_start - start:10.1f}s")

    Path(args.json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print()
    print(f"  written to {args.json}")
    print()
    print("  'setup room' is how long the heading sits still before output begins.")
    print("  'static tail' is how long the finished output sits still afterwards.")
    print("  Both are where narration can be given more time by freezing the frame.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
