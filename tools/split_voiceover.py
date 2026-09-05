"""Cut one continuous narration file into the sixteen cue clips.

Pasting the whole script in one go is much less work than generating sixteen clips, but
laying a continuous take over the video is the thing that failed before: within each beat
the voice was still explaining what was about to happen while the terminal had already
moved on.

This gets both. The continuous file is transcribed, the transcript is aligned against the
scripted clips, and the audio is cut at the word boundaries where one clip ends and the
next begins. The pieces then go through the normal placement path, so each one lands in
its own window and drift cannot accumulate.

Splitting on silence was the obvious alternative and it is not reliable: the pause between
two clips looks exactly like the pause between two sentences inside a clip. Padding the
script with break tags to make those pauses distinguishable is worse, because ElevenLabs
documents that many break tags in one generation cause the model to speed up or add
artefacts. Aligning to the words avoids needing a marker at all.

    python tools/split_voiceover.py media/narration.mp3
    python tools/split_voiceover.py media/part1.mp3 media/part2.mp3   # if split by limit
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from patch_voice import FFMPEG, duration, run  # noqa: E402
from transcribe import load_script, normalise  # noqa: E402


def transcribe_words(paths: list[Path], model_name: str) -> list[tuple[float, str]]:
    """Recognised words with absolute timestamps across one or more files."""
    from faster_whisper import WhisperModel

    print(f"loading model {model_name} ...", flush=True)
    model = WhisperModel(model_name, device="cpu", compute_type="int8")

    words: list[tuple[float, str]] = []
    base = 0.0
    for path in paths:
        wav = Path("media/_split.wav")
        run([FFMPEG, "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
             "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
        print(f"transcribing {path.name} ...", flush=True)
        segments, _ = model.transcribe(str(wav), language="en", vad_filter=False,
                                       beam_size=5, word_timestamps=True)
        for segment in segments:
            for word in segment.words:
                for token in normalise(word.word):
                    words.append((base + word.start, token))
        base += duration(path)
        wav.unlink(missing_ok=True)
    return words


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", nargs="+", help="the continuous narration file(s)")
    parser.add_argument("--script", default="VOICEOVER.md")
    parser.add_argument("--cues", default="media/cues.json")
    parser.add_argument("--vo", default="media/vo")
    parser.add_argument("--model", default="base.en")
    args = parser.parse_args(argv)

    paths = [Path(p) for p in args.audio]
    for path in paths:
        if not path.exists():
            raise SystemExit(f"{path} not found")

    script = load_script(Path(args.script))
    cues = [c for c in json.loads(Path(args.cues).read_text(encoding="utf-8"))
            if c["id"] in script]

    # One long expected token stream, remembering which clip each token came from.
    expected: list[str] = []
    owner: list[str] = []
    for cue in cues:
        tokens = normalise(script[cue["id"]])
        expected.extend(tokens)
        owner.extend([cue["id"]] * len(tokens))

    heard = transcribe_words(paths, args.model)
    recognised = [token for _, token in heard]
    print(f"  {len(recognised)} words recognised, {len(expected)} words scripted")

    # Monotonic alignment. Only matching blocks are trusted; recognition errors simply
    # leave a gap, and a clip's boundary is taken from the nearest word that did match.
    matcher = difflib.SequenceMatcher(None, expected, recognised, autojunk=False)
    mapping: dict[int, int] = {}
    matched = 0
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            mapping[block.a + offset] = block.b + offset
            matched += 1
    share = matched / max(1, len(expected))
    print(f"  aligned {share * 100:.0f}% of scripted words")
    if share < 0.5:
        raise SystemExit(
            "less than half the script could be aligned, so the cuts would be "
            "guesswork.\nCheck that the audio really is this script, and try "
            "--model small.en for better recognition.")

    folder = Path(args.vo)
    folder.mkdir(parents=True, exist_ok=True)

    total = sum(duration(p) for p in paths)
    print()
    print(f"  {'cue':5s} {'cut from audio':>18s} {'length':>8s} {'window':>8s}  fit")
    print("  " + "-" * 60)

    problems: list[str] = []
    for index, cue in enumerate(cues):
        indices = [i for i, who in enumerate(owner) if who == cue["id"]]
        present = [i for i in indices if i in mapping]
        if not present:
            problems.append(f"{cue['id']} not found")
            print(f"  {cue['id']:5s} {'not found in the audio':>18s}")
            continue

        begin = heard[mapping[present[0]]][0]
        end_word = heard[mapping[present[-1]]][0]

        # The stored timestamp is the start of the last word, so run on to whichever
        # comes first: the next clip's first word, or a short tail.
        next_start = None
        for later in range(index + 1, len(cues)):
            later_present = [i for i, who in enumerate(owner)
                             if who == cues[later]["id"] and i in mapping]
            if later_present:
                next_start = heard[mapping[later_present[0]]][0]
                break
        finish = min(next_start - 0.05, end_word + 1.2) if next_start else \
            min(total, end_word + 1.2)
        begin = max(0.0, begin - 0.15)
        if finish <= begin + 0.3:
            problems.append(f"{cue['id']} span too short")
            print(f"  {cue['id']:5s} span collapsed, skipped")
            continue

        # Cutting from a concatenation of several files is only correct inside one file.
        source, local = paths[0], begin
        running = 0.0
        for path in paths:
            length = duration(path)
            if begin < running + length or path is paths[-1]:
                source, local = path, begin - running
                finish = finish - running
                break
            running += length

        out = folder / f"{cue['id']}.wav"
        run([FFMPEG, "-y", "-v", "error", "-ss", f"{local:.3f}",
             "-to", f"{finish:.3f}", "-i", str(source), "-vn",
             "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(out)])

        length = finish - local
        room = cue["seconds"]
        if length > room + 0.05:
            fit = f"over by {length - room:.1f}s, will be sped up"
        else:
            fit = f"fits, {room - length:.1f}s spare"
        print(f"  {cue['id']:5s} {local:7.1f}-{finish:<7.1f} {length:7.1f}s "
              f"{room:7.1f}s  {fit}")

    print()
    if problems:
        print(f"  {len(problems)} problem(s): {', '.join(problems)}")
        print("  Those windows will be quiet unless the clips are supplied by hand.")
    else:
        print(f"  all {len(cues)} clips written to {folder}")
    print()
    print("Now place them and check the result. --no-trim matters: these clips are")
    print("already cut at word boundaries, and trimming them again eats real words.")
    print(f"  python tools/sync_voiceover.py --vo {folder} --no-trim")
    print("  python tools/transcribe.py --video media/final_tts.mp4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
