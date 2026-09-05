"""Inspect a voice recording against the cue sheet before using it.

Reports what can be measured without listening: level, noise floor, clipping, and
whether every cue window actually contains speech. A window with no speech means a line
was skipped; speech running well past a window means the read drifted and later lines
will land on the wrong screen.

This does not check the words. Verifying wording needs speech recognition, which is
handled separately in tools/transcribe.py.

    python tools/check_recording.py media/voice_raw.aac
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

SR = 16_000          # plenty for level analysis
BLOCK = 0.05         # seconds per RMS block


def find_tool(name: str, fallback: str) -> str:
    if shutil.which(name):
        return name
    if Path(fallback).exists():
        return fallback
    raise SystemExit(f"{name} not found")


def load_mono(ffmpeg: str, path: Path) -> list[float]:
    raw = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "1",
         "-ar", str(SR), "-f", "s16le", "-"],
        capture_output=True, check=True).stdout
    count = len(raw) // 2
    return list(struct.unpack(f"<{count}h", raw[: count * 2]))


def rms_blocks(samples: list[int]) -> list[float]:
    step = int(SR * BLOCK)
    out: list[float] = []
    for start in range(0, len(samples) - step, step):
        chunk = samples[start : start + step]
        total = 0
        for value in chunk:
            total += value * value
        rms = math.sqrt(total / len(chunk))
        out.append(-120.0 if rms == 0 else 20 * math.log10(rms / 32768))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", nargs="?", default="media/voice_raw.aac")
    parser.add_argument("--cues", default="media/cues.json")
    parser.add_argument("--video", default="media/video_clean.mp4")
    args = parser.parse_args(argv)

    ffmpeg = find_tool("ffmpeg", r"C:\ffmpeg\bin\ffmpeg.exe")
    ffprobe = find_tool("ffprobe", r"C:\ffmpeg\bin\ffprobe.exe")
    audio = Path(args.audio)
    if not audio.exists():
        raise SystemExit(f"{audio} not found")

    def duration(path: Path) -> float:
        return float(subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, check=True).stdout.strip())

    audio_len = duration(audio)
    video_len = duration(Path(args.video)) if Path(args.video).exists() else 0.0

    print(f"recording {audio.name}")
    print(f"  {audio_len:.2f}s   video {video_len:.2f}s   "
          f"difference {audio_len - video_len:+.2f}s")

    samples = load_mono(ffmpeg, audio)
    blocks = rms_blocks(samples)
    peak = max(abs(v) for v in samples) / 32768
    loud = sorted(blocks, reverse=True)
    speech_level = sum(loud[: max(1, len(loud) // 5)]) / max(1, len(loud) // 5)
    quiet = sorted(blocks)
    noise_floor = sum(quiet[: max(1, len(quiet) // 10)]) / max(1, len(quiet) // 10)

    print()
    print("  level")
    print(f"    true peak            {20 * math.log10(peak):.1f} dBFS"
          + ("   CLIPPING" if peak > 0.995 else ""))
    print(f"    speech level         {speech_level:.1f} dBFS (loudest fifth)")
    print(f"    noise floor          {noise_floor:.1f} dBFS (quietest tenth)")
    print(f"    signal to noise      {speech_level - noise_floor:.1f} dB")
    if speech_level - noise_floor < 25:
        print("    NOTE: less than 25 dB of headroom over the noise; denoise will help")

    # Speech presence per cue window.
    threshold = noise_floor + 12
    def has_speech(start: float, end: float) -> float:
        first, last = int(start / BLOCK), min(int(end / BLOCK), len(blocks) - 1)
        if last <= first:
            return 0.0
        window = blocks[first:last]
        return sum(1 for v in window if v > threshold) / len(window)

    cues = json.loads(Path(args.cues).read_text(encoding="utf-8"))
    terminal_len = 0.0
    timeline = Path("media/terminal.timeline.json")
    if timeline.exists() and Path("media/terminal.mp4").exists():
        terminal_len = duration(Path("media/terminal.mp4"))

    print()
    print("  speech coverage per cue window (share of the window with voice in it)")
    print(f"  {'cue':6s} {'window':>15s} {'coverage':>9s}")
    print("  " + "-" * 36)
    problems: list[str] = []
    for cue in cues:
        offset = terminal_len if cue["phase"] == "browser" else 0.0
        start, end = cue["from"] + offset, cue["to"] + offset
        coverage = has_speech(start, end)
        flag = ""
        if coverage < 0.15:
            flag = "  SILENT - line likely missed"
            problems.append(f"{cue['id']} silent")
        elif coverage < 0.35:
            flag = "  thin"
        print(f"  {cue['id']:6s} {start:6.1f}-{end:<6.1f} {coverage * 100:8.0f}%{flag}")

    print()
    if problems:
        print(f"  {len(problems)} window(s) need attention: {', '.join(problems)}")
    else:
        print("  every cue window contains speech")
    print()
    print("  Wording is not checked here. Run tools/transcribe.py for that.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
