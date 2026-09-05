"""Find beat boundaries in recorded footage, by measuring per-frame ink.

Scene detection does not work on this material. Every frame is pale text on a near
black background, so consecutive frames are almost identical by any histogram
measure and the scene score never crosses a useful threshold.

What does work is the demo's own structure. Each beat begins by clearing the screen,
so the amount of lit pixels collapses to almost nothing and then climbs as output is
revealed. Sampling mean luma at a few frames per second and looking for sharp drops
into the low band finds those clears precisely, which is what the narration script
needs to be timed against.

    python tools/analyse_footage.py media/walkthrough.mp4
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# 10 fps is enough to catch a cleared screen without decoding every frame. At 4 fps
# two of the seven clears fell between samples and went undetected.
SAMPLE_FPS = 10
WIDTH, HEIGHT = 160, 90
FRAME_BYTES = WIDTH * HEIGHT

# Comfortable narration speed. Used only to turn a segment length into a word budget,
# so the script can be written to fit the footage rather than trimmed afterwards.
WORDS_PER_MINUTE = 145


def find_ffmpeg(explicit: str | None = None) -> str:
    for candidate in (explicit, "ffmpeg", r"C:\ffmpeg\bin\ffmpeg.exe"):
        if not candidate:
            continue
        if candidate in ("ffmpeg",) and shutil.which(candidate):
            return candidate
        if Path(candidate).exists():
            return candidate
    raise SystemExit("ffmpeg not found; pass --ffmpeg")


def sample_ink(ffmpeg: str, video: Path) -> list[float]:
    """Mean luma per sampled frame, 0 for a black screen."""
    command = [
        ffmpeg, "-v", "error", "-i", str(video),
        "-vf", f"fps={SAMPLE_FPS},scale={WIDTH}:{HEIGHT},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr.decode("utf-8", "replace")[:800])
    raw = result.stdout
    count = len(raw) // FRAME_BYTES
    return [
        sum(raw[i * FRAME_BYTES : (i + 1) * FRAME_BYTES]) / FRAME_BYTES
        for i in range(count)
    ]


def find_clears(
    ink: list[float], sample_fps: int, min_gap_s: float = 6.0, band: float = 0.18
) -> list[float]:
    """Timestamps where the screen was cleared.

    Detected by clustering samples that sit in the lowest band of the ink curve,
    rather than by looking for a drop. A freshly cleared screen holds only a five
    line heading, so it is the global minimum of the whole take; every such trough is
    one beat start. Looking for a *drop* instead missed beats whose output arrives
    fast enough that the blank frame falls between two samples.
    """
    if not ink:
        return []
    low, high = min(ink), max(ink)
    threshold = low + band * (high - low or 1.0)

    troughs: list[float] = []
    run_start: int | None = None
    for index, value in enumerate(ink + [high]):
        if value <= threshold:
            if run_start is None:
                run_start = index
        elif run_start is not None:
            # Take the start of the run: that is the moment the screen went blank.
            moment = run_start / sample_fps
            if not troughs or moment - troughs[-1] > min_gap_s:
                troughs.append(moment)
            run_start = None
    return troughs


def timecode(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", nargs="?", default="media/walkthrough.mp4")
    parser.add_argument("--ffmpeg", default=None)
    parser.add_argument(
        "--band",
        type=float,
        default=0.15,
        help="Fraction of the ink range treated as a blank screen. Default 0.15.",
    )
    parser.add_argument("--csv", default="", help="Optional path to dump the ink curve.")
    args = parser.parse_args(argv)

    video = Path(args.video)
    if not video.exists():
        raise SystemExit(f"{video} does not exist")

    ffmpeg = find_ffmpeg(args.ffmpeg)
    ink = sample_ink(ffmpeg, video)
    duration = len(ink) / SAMPLE_FPS
    print(f"{video}: {len(ink)} samples at {SAMPLE_FPS} fps, {duration:.1f}s covered")
    print(f"ink range {min(ink):.2f} to {max(ink):.2f}")

    cuts = find_clears(ink, SAMPLE_FPS, band=args.band)
    # The first trough is the black frame before anything is drawn, not a beat.
    if cuts and cuts[0] < 1.0:
        cuts = cuts[1:]

    boundaries = cuts + [duration]
    print()
    print(f"{len(cuts)} screen clears detected. Segments:")
    print()
    print(f"  {'segment':22s} {'from':>6s} {'to':>6s} {'secs':>6s} {'words':>6s}")
    print("  " + "-" * 50)

    titles = [
        "1 the problem",
        "2 reconcile a batch",
        "3 arithmetic by hand",
        "4 verification gate",
        "5 the false positive",
        "6 does it generalise",
        "7 agent is graded",
    ]

    if cuts and cuts[0] > 1.0:
        print(
            f"  {'title card':22s} {timecode(0):>6s} {timecode(cuts[0]):>6s}"
            f" {cuts[0]:6.1f} {int(cuts[0] / 60 * WORDS_PER_MINUTE):6d}"
        )
    for index, start in enumerate(cuts):
        end = boundaries[index + 1]
        label = titles[index] if index < len(titles) else f"segment {index + 1}"
        length = end - start
        print(
            f"  {label:22s} {timecode(start):>6s} {timecode(end):>6s}"
            f" {length:6.1f} {int(length / 60 * WORDS_PER_MINUTE):6d}"
        )
    print()
    print(
        f"  total {duration:.1f}s. Word budgets assume {WORDS_PER_MINUTE} wpm, which is"
    )
    print("  an unhurried speaking pace for technical material.")

    if args.csv:
        Path(args.csv).write_text(
            "\n".join(f"{i / SAMPLE_FPS:.2f},{v:.4f}" for i, v in enumerate(ink)),
            encoding="utf-8",
        )
        print(f"\nink curve written to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
