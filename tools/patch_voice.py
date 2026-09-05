"""Replace the narration inside one or more cue windows without re-recording the rest.

Re-reading four and a half minutes to fix one sentence is a bad trade near a deadline.
Each beat of the footage holds still while its line is spoken, so the audio inside a
single cue window can be swapped for a fresh take and nothing else moves.

The replacement is trimmed or padded to the exact window length, which keeps the total
duration identical. That matters: if the patch changed the length, every cue after it
would slide out of sync.

    # record just the one line on your phone, drop it in, then:
    python tools/patch_voice.py --patch 07a=media/retake_07a.m4a
    python tools/patch_voice.py --patch 02b=media/r1.m4a --patch 07a=media/r2.m4a
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

CLEAN_CHAIN = (
    "highpass=f=85,"
    "afftdn=nr=10:nf=-45,"
    "equalizer=f=300:t=q:w=1.1:g=-3,"
    "equalizer=f=3500:t=q:w=1.4:g=2.5,"
    "equalizer=f=7200:t=q:w=1.6:g=-2,"
    "acompressor=threshold=-20dB:ratio=3:attack=8:release=200:makeup=2,"
    "loudnorm=I=-16:TP=-1.5:LRA=9"
)


def tool(name: str, fallback: str) -> str:
    if shutil.which(name):
        return name
    if Path(fallback).exists():
        return fallback
    raise SystemExit(f"{name} not found")


FFMPEG = tool("ffmpeg", r"C:\ffmpeg\bin\ffmpeg.exe")
FFPROBE = tool("ffprobe", r"C:\ffmpeg\bin\ffprobe.exe")


def duration(path: Path) -> float:
    return float(subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True).stdout.strip())


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg failed:\n{result.stderr[-1500:]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patch", action="append", required=True,
                        metavar="ID=FILE", help="cue id and its replacement recording")
    parser.add_argument("--video", default="media/video_clean.mp4")
    parser.add_argument("--audio", default="media/voice_raw.aac")
    parser.add_argument("--cues", default="media/cues.json")
    parser.add_argument("--out", default="media/final.mp4")
    args = parser.parse_args(argv)

    cues = {c["id"]: c for c in json.loads(
        Path(args.cues).read_text(encoding="utf-8"))}
    terminal_len = duration(Path("media/terminal.mp4"))
    video_len = duration(Path(args.video))

    patches: list[tuple[str, Path, float, float]] = []
    for item in args.patch:
        if "=" not in item:
            raise SystemExit(f"--patch wants ID=FILE, got {item!r}")
        cue_id, filename = item.split("=", 1)
        if cue_id not in cues:
            raise SystemExit(f"unknown cue {cue_id!r}; known: {', '.join(cues)}")
        replacement = Path(filename)
        if not replacement.exists():
            raise SystemExit(f"{replacement} not found")
        cue = cues[cue_id]
        offset = terminal_len if cue["phase"] == "browser" else 0.0
        patches.append((cue_id, replacement, cue["from"] + offset,
                        cue["to"] + offset))
    patches.sort(key=lambda p: p[2])

    work = Path("media/_patch")
    work.mkdir(exist_ok=True)

    # Clean the original take once, into a WAV so the later cutting is sample exact.
    base = work / "base.wav"
    print("cleaning original take ...")
    run([FFMPEG, "-y", "-v", "error", "-i", args.audio, "-vn",
         "-af", CLEAN_CHAIN, "-ac", "1", "-ar", "48000",
         "-c:a", "pcm_s16le", str(base)])

    # Build the timeline: untouched stretches interleaved with padded replacements.
    pieces: list[Path] = []
    cursor = 0.0
    for index, (cue_id, replacement, start, end) in enumerate(patches):
        window = end - start
        if start > cursor:
            keep = work / f"keep{index}.wav"
            run([FFMPEG, "-y", "-v", "error", "-i", str(base),
                 "-ss", f"{cursor:.3f}", "-to", f"{start:.3f}",
                 "-c:a", "pcm_s16le", str(keep)])
            pieces.append(keep)

        got = duration(replacement)
        fixed = work / f"patch_{cue_id}.wav"
        # apad then atrim guarantees exactly the window length whether the retake ran
        # long or short. A retake that overruns is cut off, so leave a little room.
        run([FFMPEG, "-y", "-v", "error", "-i", str(replacement), "-vn",
             "-af", f"{CLEAN_CHAIN},apad,atrim=0:{window:.3f}",
             "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(fixed)])
        note = "fits" if got <= window + 0.05 else f"TRIMMED, retake is {got:.1f}s"
        print(f"  {cue_id}  window {window:.1f}s  retake {got:.1f}s  {note}")
        pieces.append(fixed)
        cursor = end

    tail_end = max(cursor, video_len)
    if tail_end > cursor:
        tail = work / "tail.wav"
        run([FFMPEG, "-y", "-v", "error", "-i", str(base),
             "-ss", f"{cursor:.3f}", "-c:a", "pcm_s16le", str(tail)])
        pieces.append(tail)

    listing = work / "list.txt"
    listing.write_text("".join(
        f"file '{p.resolve().as_posix()}'\n" for p in pieces), encoding="utf-8")
    joined = work / "joined.wav"
    # Concatenating PCM is safe; the earlier warning about -c copy applies to MP4.
    run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c:a", "pcm_s16le", str(joined)])

    made = duration(joined)
    print(f"patched track {made:.2f}s against video {video_len:.2f}s "
          f"({made - video_len:+.2f}s)")
    if abs(made - video_len) > 1.0:
        print("  WARNING: length drifted by more than a second; cues after the last")
        print("  patch may no longer line up")

    print("muxing ...")
    run([FFMPEG, "-y", "-v", "error", "-i", args.video, "-i", str(joined),
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest",
         str(args.out)])

    # Container metadata has lied before. Decode the whole thing and fail on any
    # decoder complaint at all.
    check = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(args.out), "-f", "null", "-"],
        capture_output=True, text=True)
    if check.stderr.strip():
        print("DECODE ERRORS:")
        print(check.stderr[-1500:])
        return 1

    out = Path(args.out)
    total = duration(out)
    print(f"  {out}  {int(total // 60)}:{total % 60:04.1f}, "
          f"{out.stat().st_size / 1_000_000:.1f} MB, decoded clean")
    shutil.rmtree(work, ignore_errors=True)
    print()
    print("Re-run tools/transcribe.py to confirm the patched lines read correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
