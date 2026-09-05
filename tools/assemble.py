"""Assemble the final video from footage, a cue sheet, and per-cue narration clips.

Input
-----
``media/terminal.mp4``   the walkthrough, silent
``media/browser.mp4``    the dashboard section, silent
``media/cues.json``      windows measured by tools/cue_sheet.py
``media/vo/<id>.mp3``    one narration clip per cue id

How it places narration
-----------------------
Each cue is a window in the footage where the screen is deliberately still: either the
heading before a command runs, or the finished output afterwards. A clip is placed at the
start of its window.

If a clip is longer than its window, the video freezes on the window's last frame for the
difference and everything after shifts later. Freezing is invisible here because the
terminal was static through that window by design. If a clip is shorter, the remaining
stillness is simply quiet, which is better than filling it.

Nothing is sped up, nothing is pitch-shifted, and no footage is trimmed.

Missing clips are reported and skipped, so a partial set still assembles and can be
reviewed.

    python tools/assemble.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def find_tool(name: str, fallback: str) -> str:
    if shutil.which(name):
        return name
    if Path(fallback).exists():
        return fallback
    raise SystemExit(f"{name} not found")


def duration_of(ffprobe: str, path: Path) -> float:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def timecode(seconds: float) -> str:
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terminal", default="media/terminal.mp4")
    parser.add_argument("--browser", default="media/browser.mp4")
    parser.add_argument("--cues", default="media/cues.json")
    parser.add_argument("--vo", default="media/vo")
    parser.add_argument("--out", default="media/final.mp4")
    parser.add_argument("--crf", type=int, default=20)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    ffmpeg = find_tool("ffmpeg", r"C:\ffmpeg\bin\ffmpeg.exe")
    ffprobe = find_tool("ffprobe", r"C:\ffmpeg\bin\ffprobe.exe")

    terminal = Path(args.terminal)
    browser = Path(args.browser)
    vo_dir = Path(args.vo)
    if not terminal.exists():
        raise SystemExit(f"{terminal} not found")
    cues = json.loads(Path(args.cues).read_text(encoding="utf-8"))

    terminal_len = duration_of(ffprobe, terminal)
    print(f"terminal {terminal_len:.2f}s")

    def clip_for(cue_id: str) -> Path | None:
        for suffix in (".mp3", ".wav", ".m4a"):
            candidate = vo_dir / f"{cue_id}{suffix}"
            if candidate.exists():
                return candidate
        return None

    # -- work out where the video must be held open --------------------------
    terminal_cues = [c for c in cues if c["phase"] != "browser"]
    browser_cue = next((c for c in cues if c["phase"] == "browser"), None)

    holds: list[tuple[float, float, str]] = []   # (at, extra_seconds, cue id)
    placements: list[tuple[str, Path, float]] = []  # (cue id, clip, raw offset)
    missing: list[str] = []

    for cue in terminal_cues:
        clip = clip_for(cue["id"])
        if clip is None:
            missing.append(cue["id"])
            continue
        clip_len = duration_of(ffprobe, clip)
        window = cue["to"] - cue["from"]
        placements.append((cue["id"], clip, cue["from"]))
        if clip_len > window + 0.05:
            holds.append((cue["to"], clip_len - window, cue["id"]))

    print(f"{len(placements)} clips found, {len(missing)} missing"
          + (f": {missing}" if missing else ""))
    if not placements:
        raise SystemExit(f"no narration clips in {vo_dir}. See VOICEOVER.md.")

    holds.sort()
    print()
    print(f"  {'cue':6s} {'window':>16s} {'clip':>7s} {'hold':>7s}")
    print("  " + "-" * 42)
    hold_by_cue = {cue_id: extra for _, extra, cue_id in holds}
    for cue in terminal_cues:
        clip = clip_for(cue["id"])
        if clip is None:
            print(f"  {cue['id']:6s} {'-- missing clip --':>16s}")
            continue
        clip_len = duration_of(ffprobe, clip)
        extra = hold_by_cue.get(cue["id"], 0.0)
        print(f"  {cue['id']:6s} "
              f"{timecode(cue['from']) + '-' + timecode(cue['to']):>16s} "
              f"{clip_len:6.1f}s {extra:+6.1f}s")

    # -- build the video: terminal split at each hold, then browser ----------
    cuts = [0.0] + [at for at, _, _ in holds] + [terminal_len]
    inputs: list[str] = []
    chains: list[str] = []
    labels: list[str] = []
    index = 0
    shift_at: list[tuple[float, float]] = []
    cumulative = 0.0

    for segment in range(len(cuts) - 1):
        start, stop = cuts[segment], cuts[segment + 1]
        if stop - start <= 0.02:
            continue
        inputs += ["-ss", f"{start:.3f}", "-t", f"{stop - start:.3f}", str(terminal)]
        extra = holds[segment][1] if segment < len(holds) else 0.0
        filters = ["fps=24", "setpts=PTS-STARTPTS"]
        if extra > 0.01:
            filters.append(f"tpad=stop_mode=clone:stop_duration={extra:.3f}")
        chains.append(f"[{index}:v]{','.join(filters)}[v{index}]")
        labels.append(f"[v{index}]")
        index += 1
        if extra > 0.01:
            cumulative += extra
            shift_at.append((stop, cumulative))

    browser_offset = terminal_len + cumulative
    if browser.exists():
        inputs += ["-i" if False else "-ss", "0", "-t",
                   f"{duration_of(ffprobe, browser):.3f}", str(browser)]
        chains.append(f"[{index}:v]fps=24,setpts=PTS-STARTPTS[v{index}]")
        labels.append(f"[v{index}]")
        index += 1

    # Insert the -i flags that the loop above deliberately left out, so each
    # -ss/-t pair binds to its own input rather than to the previous one.
    resolved: list[str] = []
    position = 0
    while position < len(inputs):
        resolved += [inputs[position], inputs[position + 1],
                     inputs[position + 2], inputs[position + 3], "-i", inputs[position + 4]]
        position += 5

    def shifted(moment: float) -> float:
        total = 0.0
        for at, amount in shift_at:
            if moment >= at - 0.001:
                total = amount
        return moment + total

    # -- audio: place each clip at its shifted offset ------------------------
    audio_inputs: list[str] = []
    audio_chains: list[str] = []
    audio_labels: list[str] = []
    for order, (cue_id, clip, raw_offset) in enumerate(placements):
        audio_inputs += ["-i", str(clip)]
        stream = index + order
        delay = int(shifted(raw_offset) * 1000)
        audio_chains.append(
            f"[{stream}:a]aresample=48000,adelay={delay}|{delay}[a{order}]"
        )
        audio_labels.append(f"[a{order}]")

    if browser_cue is not None:
        clip = clip_for(browser_cue["id"])
        if clip is not None:
            audio_inputs += ["-i", str(clip)]
            stream = index + len(placements)
            delay = int(browser_offset * 1000)
            audio_chains.append(
                f"[{stream}:a]aresample=48000,adelay={delay}|{delay}[abr]"
            )
            audio_labels.append("[abr]")
        else:
            missing.append(browser_cue["id"])

    chains.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[vout]")
    chains += audio_chains
    chains.append(
        f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:normalize=0,"
        f"loudnorm=I=-16:TP=-1.5:LRA=11,aformat=channel_layouts=stereo[aout]"
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-v", "error"] + resolved + audio_inputs + [
        "-filter_complex", ";".join(chains),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf),
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", str(out),
    ]
    if args.verbose:
        print()
        print(" ".join(command))

    print()
    print(f"  total freeze added {cumulative:.1f}s")
    print(f"  browser starts at {timecode(browser_offset)}")
    print("  encoding in a single pass...")
    subprocess.run(command, check=True)

    # Container metadata is not evidence of playability, so decode it all.
    check = subprocess.run([ffmpeg, "-v", "error", "-i", str(out), "-f", "null", "-"],
                           capture_output=True, text=True)
    if check.stderr.strip():
        for line in check.stderr.strip().splitlines()[:5]:
            print(f"    {line}")
        raise SystemExit("output is not a clean bitstream")

    final = duration_of(ffprobe, out)
    print(f"  wrote {out}  {final:.1f}s ({timecode(final)}), "
          f"{out.stat().st_size / 1_048_576:.1f} MB, decoded clean")
    if missing:
        print(f"  NOTE: no clip for {missing}; those windows are silent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
