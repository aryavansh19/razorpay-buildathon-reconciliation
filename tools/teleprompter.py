"""Build a read-along version of the video with the narration burned in.

Play this full screen, record your voice on a phone, and read each line as it appears.
The result is a voice track already in time with the picture, so assembly is a mux
rather than an edit.

Pacing is the point. Each cue's text is split into sentences and each sentence is shown
for a slice of the window proportional to its word count, so the prompt itself sets the
speed. A whole paragraph dumped on screen for twelve seconds gives no such guidance, and
that is how a read-along drifts out of time with the footage.

Two lines are shown at once. The lower band is what to say now. The upper, dimmer line is
what is coming, appearing a couple of seconds early so there is no cold start on a new
sentence.

    python tools/teleprompter.py
    # then play media/teleprompter.mp4 full screen and read aloud
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

LEAD_IN = 2.2          # seconds a line is previewed before it is due
MIN_ON_SCREEN = 1.4    # never flash a sentence faster than this


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


def load_script(path: Path) -> dict[str, str]:
    """Pull the narration text for each cue id out of VOICEOVER.md."""
    fence = "`" * 3
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    clips: dict[str, str] = {}
    current, inside, buf = None, False, []
    for line in text.split("\n"):
        heading = re.match(r"^### `([\w]+)`", line)
        if heading:
            current = heading.group(1)
        if line.strip() == fence:
            if inside:
                if current:
                    body = " ".join(buf)
                    body = re.sub(r"\[[^\]]+\]", "", body)      # drop audio tags
                    body = re.sub(r"<break[^>]*/>", "", body)   # drop break tags
                    clips[current] = re.sub(r"\s+", " ", body).strip()
                buf, inside = [], False
            else:
                inside = True
        elif inside:
            buf.append(line)
    return clips


def sentences_of(text: str) -> list[str]:
    parts = re.split(r"(?<=[.:?])\s+", text)
    merged: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Keep very short fragments attached to the previous line rather than
        # flashing them on their own.
        if merged and len(part.split()) <= 3:
            merged[-1] += " " + part
        else:
            merged.append(part)
    return merged


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


def escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def wrap(text: str, width: int = 78) -> str:
    words, lines, line = text.split(), [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if len(candidate) > width and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return "\\N".join(lines[:3])


HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Say,Segoe UI Semibold,46,&H00FFFFFF,&H00FFFFFF,&H00101010,&HC0000000,0,0,0,0,100,100,0,0,3,4,0,2,70,70,48,1
Style: Next,Segoe UI,30,&H00B0B0B0,&H00B0B0B0,&H00101010,&HA0000000,0,0,0,0,100,100,0,0,3,3,0,8,70,70,40,1
Style: Cue,Consolas,26,&H0070D8FF,&H0070D8FF,&H00101010,&HA0000000,0,0,0,0,100,100,0,0,3,3,0,7,40,40,26,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(cues: list[dict], clips: dict[str, str], browser_offset: float,
              out: Path) -> tuple[int, list[str]]:
    events: list[str] = []
    missing: list[str] = []
    count = 0

    for cue in cues:
        cue_id = cue["id"]
        text = clips.get(cue_id)
        if not text:
            missing.append(cue_id)
            continue

        start = cue["from"] + (browser_offset if cue["phase"] == "browser" else 0.0)
        end = cue["to"] + (browser_offset if cue["phase"] == "browser" else 0.0)
        window = max(0.5, end - start)

        chunks = sentences_of(text)
        weights = [max(1, len(chunk.split())) for chunk in chunks]
        total_weight = sum(weights)

        # A label so a retake can be aimed at one cue instead of the whole video.
        events.append(
            f"Dialogue: 0,{ass_time(start - LEAD_IN)},{ass_time(end)},Cue,,0,0,0,,"
            f"{cue_id}  {escape(cue['title'])[:44]}  ({window:.0f}s)"
        )

        cursor = start
        for index, (chunk, weight) in enumerate(zip(chunks, weights)):
            slice_len = max(MIN_ON_SCREEN, window * weight / total_weight)
            chunk_end = min(end, cursor + slice_len) if index < len(chunks) - 1 else end
            events.append(
                f"Dialogue: 1,{ass_time(cursor)},{ass_time(chunk_end)},Say,,0,0,0,,"
                f"{wrap(escape(chunk))}"
            )
            if index + 1 < len(chunks):
                nxt = chunks[index + 1]
                events.append(
                    f"Dialogue: 0,{ass_time(max(cursor, chunk_end - LEAD_IN))},"
                    f"{ass_time(chunk_end)},Next,,0,0,0,,next:  {wrap(escape(nxt), 92)}"
                )
            cursor = chunk_end
            count += 1

        # Preview the opening sentence before the window opens.
        events.append(
            f"Dialogue: 0,{ass_time(start - LEAD_IN)},{ass_time(start)},Next,,0,0,0,,"
            f"ready:  {wrap(escape(chunks[0]), 92)}"
        )

    out.write_text(HEADER + "\n".join(events) + "\n", encoding="utf-8", newline="\n")
    return count, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terminal", default="media/terminal.mp4")
    parser.add_argument("--browser", default="media/browser.mp4")
    parser.add_argument("--cues", default="media/cues.json")
    parser.add_argument("--script", default="VOICEOVER.md")
    parser.add_argument("--out", default="media/teleprompter.mp4")
    parser.add_argument("--crf", type=int, default=23)
    args = parser.parse_args(argv)

    ffmpeg = find_tool("ffmpeg", r"C:\ffmpeg\bin\ffmpeg.exe")
    ffprobe = find_tool("ffprobe", r"C:\ffmpeg\bin\ffprobe.exe")

    terminal, browser = Path(args.terminal), Path(args.browser)
    if not terminal.exists():
        raise SystemExit(f"{terminal} not found")
    cues = json.loads(Path(args.cues).read_text(encoding="utf-8"))
    clips = load_script(Path(args.script))

    terminal_len = duration_of(ffprobe, terminal)
    has_browser = browser.exists()
    browser_len = duration_of(ffprobe, browser) if has_browser else 0.0

    print(f"terminal {terminal_len:.1f}s" + (f" + browser {browser_len:.1f}s" if has_browser else ""))
    print(f"{len(clips)} narration blocks read from {args.script}")

    ass_path = Path("media/teleprompter.ass")
    lines, missing = build_ass(cues, clips, terminal_len, ass_path)
    print(f"{lines} prompt lines written to {ass_path}")
    if missing:
        print(f"  no script text for: {missing}")

    # ASS path has to be escaped for the filter graph on Windows.
    filter_path = str(ass_path).replace("\\", "/").replace(":", "\\:")

    inputs = ["-i", str(terminal)]
    if has_browser:
        inputs += ["-i", str(browser)]
    chains = ["[0:v]fps=24,setpts=PTS-STARTPTS[v0]"]
    labels = "[v0]"
    if has_browser:
        chains.append("[1:v]fps=24,setpts=PTS-STARTPTS[v1]")
        labels += "[v1]"
    chains.append(f"{labels}concat=n={2 if has_browser else 1}:v=1:a=0[joined]")
    chains.append(f"[joined]ass='{filter_path}'[vout]")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-v", "error"] + inputs + [
        "-filter_complex", ";".join(chains),
        "-map", "[vout]", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(args.crf),
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-movflags", "+faststart", str(out),
    ]
    print("burning in the prompt...")
    subprocess.run(command, check=True)

    check = subprocess.run([ffmpeg, "-v", "error", "-i", str(out), "-f", "null", "-"],
                           capture_output=True, text=True)
    if check.stderr.strip():
        print(check.stderr.strip()[:400])
        raise SystemExit("teleprompter render is not a clean bitstream")

    total = duration_of(ffprobe, out)
    print()
    print(f"  {out}  {int(total // 60)}:{total % 60:04.1f}, "
          f"{out.stat().st_size / 1_048_576:.1f} MB")
    print()
    print("  Play it full screen and read the lower band aloud.")
    print("  Record on your phone in the same room, one take, no need to stop.")
    print("  The upper line previews what is next so you never start cold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
