"""Place one generated narration clip per cue window and mux the result.

Each beat of the footage holds still while its line is spoken, so a clip only has to
land inside its window to be in sync. That makes synthesised narration tractable: there
is no continuous take to drift, just sixteen independent placements.

A clip that runs longer than its window is sped up to fit, capped, because the
alternative is either cutting a sentence off or letting every later clip slide. A clip
that runs short simply leaves the screen quiet, which is invisible because the terminal
is static there by design.

    python tools/sync_voiceover.py                      # reads media/vo/<id>.mp3
    python tools/sync_voiceover.py --allow-missing       # silence for clips not yet made
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from patch_voice import FFMPEG, FFPROBE, duration, run, speech_bounds  # noqa: E402

# Synthesised speech arrives clean and already levelled, so it needs none of the
# denoising and tone shaping a phone recording does. Matching loudness is the only
# thing worth doing, so the result sits at the same level as the rest of the mix.
CLEAN_CHAIN = "loudnorm=I=-16:TP=-1.5:LRA=9"

AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus")


def find_clip(folder: Path, cue_id: str) -> Path | None:
    for extension in AUDIO_EXTENSIONS:
        candidate = folder / f"{cue_id}{extension}"
        if candidate.exists():
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cues", default="media/cues.json")
    parser.add_argument("--vo", default="media/vo")
    parser.add_argument("--video", default="media/video_clean.mp4")
    parser.add_argument("--out", default="media/final_tts.mp4")
    parser.add_argument("--max-tempo", type=float, default=1.15,
                        help="refuse to speed a clip past this; beyond roughly 1.15 "
                             "it sounds hurried")
    parser.add_argument("--allow-missing", action="store_true",
                        help="leave a window quiet instead of failing when its clip "
                             "is absent")
    args = parser.parse_args(argv)

    cues = json.loads(Path(args.cues).read_text(encoding="utf-8"))
    folder = Path(args.vo)
    if not folder.exists():
        raise SystemExit(f"{folder} not found; create it and save clips as <id>.mp3")

    video = Path(args.video)
    video_len = duration(video)
    terminal_len = duration(Path("media/terminal.mp4"))

    # Resolve every cue to an absolute position in the finished cut.
    placements: list[tuple[dict, float, float, Path | None]] = []
    for cue in cues:
        offset = terminal_len if cue["phase"] == "browser" else 0.0
        placements.append((cue, cue["from"] + offset, cue["to"] + offset,
                           find_clip(folder, cue["id"])))
    placements.sort(key=lambda p: p[1])

    missing = [c["id"] for c, _, _, clip in placements if clip is None]
    if missing and not args.allow_missing:
        raise SystemExit(
            f"no clip for: {', '.join(missing)}\n"
            f"Save them in {folder} named by cue id, for example {folder}/02b.mp3, "
            f"or pass --allow-missing to leave those windows quiet.")

    work = Path("media/_vo")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    def silence(seconds: float, name: str) -> Path:
        path = work / name
        run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
             "-i", f"anullsrc=r=48000:cl=mono", "-t", f"{seconds:.3f}",
             "-c:a", "pcm_s16le", str(path)])
        return path

    pieces: list[Path] = []
    cursor = 0.0
    over: list[str] = []
    print(f"{len(placements)} cues, video {video_len:.2f}s")
    print()
    print(f"  {'cue':5s} {'window':>14s} {'room':>6s} {'clip':>7s}  fit")
    print("  " + "-" * 52)

    for index, (cue, start, end, clip) in enumerate(placements):
        window = end - start
        if start > cursor + 0.001:
            pieces.append(silence(start - cursor, f"gap{index}.wav"))

        if clip is None:
            pieces.append(silence(window, f"quiet{index}.wav"))
            print(f"  {cue['id']:5s} {start:6.1f}-{end:<7.1f} {window:5.1f}s "
                  f"{'--':>7s}  no clip, left quiet")
            cursor = end
            continue

        begin, finish = speech_bounds(clip)
        content = finish - begin
        tempo = max(1.0, content / window)
        clamped = tempo > args.max_tempo
        if clamped:
            over.append(f"{cue['id']} wanted {tempo:.2f}x")
            tempo = args.max_tempo

        chain = CLEAN_CHAIN
        if tempo > 1.001:
            chain += f",atempo={tempo:.4f}"
        chain += f",apad,atrim=0:{window:.3f}"
        fitted = work / f"cue_{cue['id']}.wav"
        run([FFMPEG, "-y", "-v", "error",
             "-ss", f"{begin:.3f}", "-to", f"{finish:.3f}", "-i", str(clip),
             "-vn", "-af", chain, "-ac", "1", "-ar", "48000",
             "-c:a", "pcm_s16le", str(fitted)])
        pieces.append(fitted)

        if clamped:
            fit = f"TOO LONG, clamped to {tempo:.2f}x and cut"
        elif tempo <= 1.001:
            fit = f"fits, {window - content:.1f}s spare"
        else:
            fit = f"sped {tempo:.3f}x"
        print(f"  {cue['id']:5s} {start:6.1f}-{end:<7.1f} {window:5.1f}s "
              f"{content:6.1f}s  {fit}")
        cursor = end

    if cursor < video_len:
        pieces.append(silence(video_len - cursor, "tail.wav"))

    listing = work / "list.txt"
    listing.write_text("".join(
        f"file '{p.resolve().as_posix()}'\n" for p in pieces), encoding="utf-8")
    track = work / "track.wav"
    run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c:a", "pcm_s16le", str(track)])

    made = duration(track)
    print()
    print(f"track {made:.2f}s against video {video_len:.2f}s ({made - video_len:+.2f}s)")
    if abs(made - video_len) > 0.5:
        print("  WARNING: the track and the picture are different lengths, so cues")
        print("  after the discrepancy will not line up")

    print("muxing ...")
    run([FFMPEG, "-y", "-v", "error", "-i", str(video), "-i", str(track),
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest",
         str(args.out)])

    # Container metadata has claimed a correct duration for an unplayable file before,
    # so decode the whole thing and treat any decoder output at all as failure.
    check = subprocess.run([FFMPEG, "-v", "error", "-i", str(args.out), "-f", "null", "-"],
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

    if over:
        print()
        print(f"  {len(over)} clip(s) had to be clamped: {', '.join(over)}")
        print("  Shorten the wording for those, or they will sound rushed.")
    if missing:
        print(f"  {len(missing)} window(s) left quiet: {', '.join(missing)}")
    print()
    print("Then check it the same way as the spoken cut:")
    print(f"  python tools/transcribe.py --video {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
