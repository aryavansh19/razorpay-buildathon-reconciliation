"""Transcribe the recorded voice track and line it up against the cue sheet.

check_recording.py proves there is speech in every cue window. It cannot tell you
whether the right words are in it. This does: it runs speech recognition over the
finished cut, buckets the words into cue windows by timestamp, and prints the
recognised text next to the scripted text so wording and fumbles are visible.

Recognition is imperfect on proper nouns and numbers, so treat a mismatch as
"look at this", not as proof of an error.

    python tools/transcribe.py                      # reads media/final.mp4
    python tools/transcribe.py --model small.en     # slower, more accurate
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


SPOKEN_NUMBERS = {
    # Recognition writes digits where the script spells numbers out, and vice versa.
    # Folding both to digits stops that difference reading as a misreading.
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
    "twelve": "12", "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50",
    "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
    "hundred": "100", "thousand": "1000",
}


def load_script(path: Path) -> dict[str, str]:
    """Pull the fenced clip text out of VOICEOVER.md, keyed by cue id.

    The clip bodies live under headings like "### `02b` reconcile, payoff" followed by
    a fenced block. Performance tags such as [pause] are stripped: they are
    instructions to a voice model, not words anybody says.
    """
    text = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    pattern = re.compile(
        r"^###\s+`(?P<id>[^`]+)`[^\n]*\n+```\n(?P<body>.*?)\n```",
        re.MULTILINE | re.DOTALL)
    for match in pattern.finditer(text):
        body = re.sub(r"\[[^\]]*\]", " ", match.group("body"))
        out[match.group("id")] = " ".join(body.split())
    return out


def normalise(text: str) -> list[str]:
    """Reduce to comparable word tokens: lowercase, no punctuation, digits spelled
    as-is, and number words folded to digits so "forty" and "40" compare equal."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    # Digits carry thousands separators in the script but not in recognition output.
    return [SPOKEN_NUMBERS.get(w, w).lstrip("0") or "0" for w in words]


def similarity(a: list[str], b: list[str]) -> float:
    """Share of scripted words that appear in the recognised text, order-free.
    Order-free on purpose: a reader who inverts a clause has not made an error worth
    flagging, but a reader who drops half a sentence has."""
    if not a:
        return 1.0
    from collections import Counter
    have = Counter(b)
    hit = 0
    for word in a:
        if have[word] > 0:
            have[word] -= 1
            hit += 1
    return hit / len(a)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", default="media/final.mp4")
    parser.add_argument("--cues", default="media/cues.json")
    parser.add_argument("--model", default="base.en")
    parser.add_argument("--out", default="media/transcript.json")
    parser.add_argument("--script", default="VOICEOVER.md")
    parser.add_argument("--reuse", action="store_true",
                        help="compare against an existing transcript instead of "
                             "running recognition again")
    args = parser.parse_args(argv)

    video = Path(args.video)
    out_path = Path(args.out)

    if args.reuse and out_path.exists():
        print(f"reusing {out_path}")
        lines = json.loads(out_path.read_text(encoding="utf-8"))
    else:
        if not video.exists():
            raise SystemExit(f"{video} not found")
        wav = Path("media/_stt.wav")
        print(f"extracting audio from {video.name} ...", flush=True)
        subprocess.run(
            [r"C:\ffmpeg\bin\ffmpeg.exe", "-y", "-v", "error", "-i", str(video),
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
            check=True)

        from faster_whisper import WhisperModel
        print(f"loading model {args.model} (downloads once) ...", flush=True)
        model = WhisperModel(args.model, device="cpu", compute_type="int8")

        print("transcribing ...", flush=True)
        # Word level timings matter here. Using the segment start for every word in the
        # segment smears a ten second span onto one instant, and words then fall into a
        # neighbouring cue's bucket, which reads as a placement error that is not real.
        segments, _ = model.transcribe(str(wav), language="en", vad_filter=True,
                                       beam_size=5, word_timestamps=True)
        lines = []
        for seg in segments:
            lines.append({
                "start": seg.start, "end": seg.end, "text": seg.text.strip(),
                "words": [{"at": w.start, "word": w.word.strip()}
                          for w in (seg.words or [])],
            })
            print(f"  [{seg.start:6.1f}s] {seg.text.strip()}", flush=True)
        out_path.write_text(json.dumps(lines, indent=2), encoding="utf-8")
        wav.unlink(missing_ok=True)

    words: list[tuple[float, str]] = []
    for line in lines:
        if line.get("words"):
            for entry in line["words"]:
                for token in normalise(entry["word"]):
                    words.append((entry["at"], token))
        else:
            # Older transcripts, saved before word timings were recorded.
            for token in normalise(line["text"]):
                words.append((line["start"], token))

    # Bucket recognised words into cue windows and compare with the script.
    cues = json.loads(Path(args.cues).read_text(encoding="utf-8"))
    terminal_len = 0.0
    if Path("media/terminal.mp4").exists():
        terminal_len = float(subprocess.run(
            [r"C:\ffmpeg\bin\ffprobe.exe", "-v", "error", "-show_entries",
             "format=duration", "-of", "default=nw=1:nk=1", "media/terminal.mp4"],
            capture_output=True, text=True, check=True).stdout.strip())

    script = load_script(Path(args.script))
    missing = [c["id"] for c in cues if c["id"] not in script]
    if missing:
        print(f"note: no script text found for {', '.join(missing)}")

    print()
    print("=" * 76)
    print("scripted line vs what was actually said")
    print("=" * 76)
    weak: list[str] = []
    for cue in cues:
        if cue["id"] not in script:
            continue
        offset = terminal_len if cue["phase"] == "browser" else 0.0
        start, end = cue["from"] + offset, cue["to"] + offset
        # A small margin only, now that timings are per word: enough to tolerate
        # recognition jitter at a boundary, not enough to borrow a neighbour's line.
        said = [w for t, w in words if start - 0.6 <= t < end + 0.6]
        want = normalise(script[cue["id"]])
        score = similarity(want, said)
        mark = "ok" if score >= 0.7 else ("thin" if score >= 0.45 else "OFF")
        if score < 0.7:
            weak.append(f"{cue['id']} ({score * 100:.0f}%)")
        print()
        print(f"[{cue['id']}] {start:.1f}-{end:.1f}s   match {score * 100:.0f}%  {mark}")
        print(f"  script: {script[cue['id']]}")
        print(f"  heard : {' '.join(said) if said else '(nothing recognised)'}")

    print()
    print("=" * 76)
    if weak:
        print(f"{len(weak)} cue(s) worth listening back to: {', '.join(weak)}")
        print("Low scores are often recognition errors on numbers and product names,")
        print("not misreadings. Check these spots by ear before re-recording anything.")
    else:
        print("every cue matches its script closely; no misreadings detected")
    print(f"full transcript written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
