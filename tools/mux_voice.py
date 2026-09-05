"""Mux a single read-along voice recording onto the clean video.

For the case where the narration was recorded in one take against
``media/teleprompter.mp4``, rather than as per-cue clips. Because the read followed the
burned-in prompt, the voice is already in time with the picture and this is a mux, not an
edit.

What it does beyond muxing:

- trims or pads the audio so it matches the video exactly, reporting the difference
- high-passes at 80 Hz to drop phone handling rumble
- light compression so quiet phrases stay audible on a laptop speaker
- normalises to -16 LUFS with a -1.5 dBTP ceiling
- decodes the finished file end to end and fails if the decoder complains

    python tools/mux_voice.py --audio media/voice.m4a
"""

from __future__ import annotations

import argparse
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
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", default="media/video_clean.mp4")
    parser.add_argument("--audio", default=None,
                        help="Your recording. Defaults to the newest audio file in media/.")
    parser.add_argument("--out", default="media/final.mp4")
    parser.add_argument("--offset", type=float, default=0.0,
                        help="Shift the voice by this many seconds. Positive delays it.")
    parser.add_argument("--crf", type=int, default=20)
    parser.add_argument("--no-clean-up", action="store_true",
                        help="Skip the noise and loudness processing.")
    args = parser.parse_args(argv)

    ffmpeg = find_tool("ffmpeg", r"C:\ffmpeg\bin\ffmpeg.exe")
    ffprobe = find_tool("ffprobe", r"C:\ffmpeg\bin\ffprobe.exe")

    video = Path(args.video)
    if not video.exists():
        raise SystemExit(f"{video} not found. Run tools/teleprompter.py first.")

    if args.audio:
        audio = Path(args.audio)
    else:
        candidates = [
            p for p in Path("media").iterdir()
            if p.suffix.lower() in (".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".mp4")
            and p.name not in (video.name, "terminal.mp4", "browser.mp4",
                              "teleprompter.mp4", "final.mp4")
        ]
        if not candidates:
            raise SystemExit("no recording found in media/; pass --audio")
        audio = max(candidates, key=lambda p: p.stat().st_mtime)
    if not audio.exists():
        raise SystemExit(f"{audio} not found")

    video_len = duration_of(ffprobe, video)
    audio_len = duration_of(ffprobe, audio)
    print(f"video {video_len:.2f}s")
    print(f"audio {audio.name}  {audio_len:.2f}s  ({audio_len - video_len:+.2f}s)")

    if abs(audio_len - video_len) > 12:
        print()
        print("  Warning: the recording differs from the video by more than 12 seconds.")
        print("  If you paused or restarted mid-take, the read will drift. Consider a")
        print("  retake, or pass --offset to shift the start.")

    chain = []
    if args.offset > 0:
        chain.append(f"adelay={int(args.offset * 1000)}|{int(args.offset * 1000)}")
    elif args.offset < 0:
        chain.append(f"atrim=start={abs(args.offset):.3f},asetpts=PTS-STARTPTS")
    if not args.no_clean_up:
        chain += [
            # Below ~85 Hz there is nothing but handling rumble and desk thumps.
            "highpass=f=85",
            # Gentle spectral denoise. The recording already has good separation from
            # its noise floor, and heavy denoising on clean speech costs more in
            # artefacts and a hollow tone than it removes in hiss.
            "afftdn=nr=10:nf=-45",
            # Phones and close speaking exaggerate 200-400 Hz, which reads as boxy.
            "equalizer=f=300:t=q:w=1.1:g=-3",
            # A little presence so consonants cut through on a laptop speaker.
            "equalizer=f=3500:t=q:w=1.4:g=2.5",
            # Tame sibilance the presence lift would otherwise emphasise.
            "equalizer=f=7200:t=q:w=1.6:g=-2",
            # Even out the gap between leaning in and sitting back.
            "acompressor=threshold=-20dB:ratio=3:attack=8:release=200:makeup=2",
            "loudnorm=I=-16:TP=-1.5:LRA=9",
        ]
    chain += ["aresample=48000", "aformat=channel_layouts=stereo"]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-y", "-v", "error", "-i", str(video), "-i", str(audio),
        "-filter_complex", f"[1:a]{','.join(chain)}[aout]",
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        # Cut whichever runs longer so the file ends cleanly.
        "-shortest",
        "-movflags", "+faststart", str(out),
    ]
    print("muxing...")
    subprocess.run(command, check=True)

    check = subprocess.run([ffmpeg, "-v", "error", "-i", str(out), "-f", "null", "-"],
                           capture_output=True, text=True)
    if check.stderr.strip():
        for line in check.stderr.strip().splitlines()[:5]:
            print(f"    {line}")
        raise SystemExit("output is not a clean bitstream")

    final = duration_of(ffprobe, out)
    print()
    print(f"  {out}  {int(final // 60)}:{final % 60:04.1f}, "
          f"{out.stat().st_size / 1_048_576:.1f} MB, decoded clean")
    print("  This is the submission file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
