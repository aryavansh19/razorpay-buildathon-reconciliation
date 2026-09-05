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
import math
import shutil
import struct
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


def speech_bounds(path: Path, sustain: float = 0.5) -> tuple[float, float]:
    """First and last moment of sustained speech in a file.

    A phone retake carries several seconds of fumbling at each end, and those seconds
    otherwise count against the window and push real words off the end.

    A fixed dB threshold is no good here: breathing and handling noise on a phone sit
    well above any level that would count as silence, so ffmpeg's silencedetect marks
    them as sound. Instead the threshold is set relative to this file's own noise
    floor, and a run has to hold for `sustain` seconds to count, which a breath or a
    knock against the desk does not.
    """
    step = 0.05
    raw = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-vn", "-ac", "1",
         "-ar", "16000", "-f", "s16le", "-"],
        capture_output=True).stdout
    count = len(raw) // 2
    samples = struct.unpack(f"<{count}h", raw[: count * 2])
    per = int(16000 * step)
    levels: list[float] = []
    for start in range(0, count - per, per):
        total = 0
        for value in samples[start : start + per]:
            total += value * value
        rms = math.sqrt(total / per)
        levels.append(-120.0 if rms == 0 else 20 * math.log10(rms / 32768))
    if not levels:
        return 0.0, duration(path)

    quiet = sorted(levels)
    floor = sum(quiet[: max(1, len(quiet) // 10)]) / max(1, len(quiet) // 10)
    threshold = floor + 12
    need = max(1, int(sustain / step))

    def first_run(sequence: list[float]) -> int | None:
        run_len = 0
        for index, level in enumerate(sequence):
            run_len = run_len + 1 if level > threshold else 0
            if run_len >= need:
                return index - run_len + 1
            
        return None

    head = first_run(levels)
    tail = first_run(levels[::-1])
    if head is None or tail is None:
        return 0.0, duration(path)
    begin = head * step
    finish = (len(levels) - tail) * step
    if finish - begin < 0.5:
        return 0.0, duration(path)
    # A little air either side so the patch does not clip the first consonant.
    return max(0.0, begin - 0.12), min(duration(path), finish + 0.18)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patch", action="append", required=True,
                        metavar="ID=FILE", help="cue id and its replacement recording")
    parser.add_argument("--extend", action="append", default=[], metavar="ID=SECONDS",
                        help="start this cue's window earlier, to buy room for a "
                             "retake that runs long. Only safe where the screen "
                             "already shows what the line talks about.")
    parser.add_argument("--speech", action="append", default=[],
                        metavar="ID=START:END",
                        help="override where the words are in a retake, in seconds, "
                             "when detection gets it wrong")
    parser.add_argument("--max-tempo", type=float, default=1.15,
                        help="refuse to speed a retake up past this, since beyond "
                             "roughly 1.15 it starts to sound hurried")
    parser.add_argument("--video", default="media/video_clean.mp4")
    parser.add_argument("--audio", default="media/voice_raw.aac")
    parser.add_argument("--cues", default="media/cues.json")
    parser.add_argument("--out", default="media/final.mp4")
    args = parser.parse_args(argv)

    cues = {c["id"]: c for c in json.loads(
        Path(args.cues).read_text(encoding="utf-8"))}
    terminal_len = duration(Path("media/terminal.mp4"))
    video_len = duration(Path(args.video))

    extensions: dict[str, float] = {}
    for item in args.extend:
        cue_id, seconds = item.split("=", 1)
        extensions[cue_id] = float(seconds)

    overrides: dict[str, tuple[float, float]] = {}
    for item in args.speech:
        cue_id, span = item.split("=", 1)
        low, high = span.split(":", 1)
        overrides[cue_id] = (float(low), float(high))

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
        start = cue["from"] + offset - extensions.get(cue_id, 0.0)
        patches.append((cue_id, replacement, start, cue["to"] + offset))
    patches.sort(key=lambda p: p[2])

    for index in range(1, len(patches)):
        if patches[index][2] < patches[index - 1][3]:
            raise SystemExit(
                f"{patches[index][0]} now starts before {patches[index - 1][0]} ends; "
                "reduce --extend")

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

        raw_len = duration(replacement)
        if cue_id in overrides:
            begin, finish = overrides[cue_id]
        else:
            begin, finish = speech_bounds(replacement)
        content = finish - begin

        # Speed up only if the words still do not fit once the dead air is gone.
        tempo = max(1.0, content / window)
        if tempo > args.max_tempo:
            raise SystemExit(
                f"{cue_id}: {content:.1f}s of speech will not fit a {window:.1f}s "
                f"window without {tempo:.2f}x speed-up (limit {args.max_tempo}).\n"
                f"Either re-read it faster, or give it room with "
                f"--extend {cue_id}={content - window + 0.4:.1f} if the screen "
                f"already shows what the line describes.")

        fixed = work / f"patch_{cue_id}.wav"
        chain = f"{CLEAN_CHAIN}"
        if tempo > 1.001:
            chain += f",atempo={tempo:.4f}"
        # apad then atrim pins the result to exactly the window length, so everything
        # after this patch stays where it was.
        chain += f",apad,atrim=0:{window:.3f}"
        run([FFMPEG, "-y", "-v", "error",
             "-ss", f"{begin:.3f}", "-to", f"{finish:.3f}", "-i", str(replacement),
             "-vn", "-af", chain, "-ac", "1", "-ar", "48000",
             "-c:a", "pcm_s16le", str(fixed)])

        detail = f"speech {content:.1f}s"
        if tempo > 1.001:
            detail += f", sped to {tempo:.3f}x"
        else:
            detail += f", {window - content:.1f}s to spare"
        print(f"  {cue_id}  {start:.1f}-{end:.1f}s ({window:.1f}s window)  "
              f"file {raw_len:.1f}s -> {detail}")
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
