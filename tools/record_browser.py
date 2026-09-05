"""Record the browser section of the pitch video.

Drives the served dashboard through a fixed script and records the viewport, so the
browser footage is as reproducible as the terminal footage. Playwright records video
itself, which avoids window management, stray cursors and anything else that happens to
be on the desktop.

The shots, in order:

1. the three headline numbers
2. the exception ledger, filterable, with reason codes and suggested actions
3. one reason code isolated, showing the settlements the gateway misreported
4. ``setl_00016`` opened, showing the netting arithmetic behind a single match
5. a live question, answered through read-only tools, with cited record identifiers

Shot 4 is the one worth having. It is the moment a reviewer can check the arithmetic by
hand instead of taking a match rate on trust.

Requires playwright, which is a tool for producing the video and deliberately not a
dependency of the project:

    pip install playwright && python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

# Full 1080p viewport, with the page itself zoomed.
#
# The obvious way to get bigger text, a small viewport recorded at a larger size, does
# not work: record_video_size sets the canvas but does not scale the page, so the
# content ends up native-size in the top-left corner with dead space around it. Zooming
# the document inside a full-size viewport enlarges the text and fills the frame, and
# because the browser re-renders at that size rather than being upscaled, the glyphs stay
# crisp. The report is laid out to a 1180px column, so at 1920 wide there is room to zoom
# into anyway.
VIEWPORT = {"width": 1920, "height": 1080}
PAGE_ZOOM = 1.45


def find_ffmpeg() -> str:
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    if Path(r"C:\ffmpeg\bin\ffmpeg.exe").exists():
        return r"C:\ffmpeg\bin\ffmpeg.exe"
    raise SystemExit("ffmpeg not found")


def wait_for_server(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.3)
    raise SystemExit(f"server did not come up on port {port}")


def record(port: int, out_dir: Path, pace: float) -> Path:
    from playwright.sync_api import sync_playwright

    def beat(seconds: float, page) -> None:
        page.wait_for_timeout(int(seconds * pace * 1000))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--force-device-scale-factor=1", "--hide-scrollbars"],
        )
        context = browser.new_context(
            viewport=VIEWPORT,
            record_video_dir=str(out_dir),
            record_video_size=VIEWPORT,
            device_scale_factor=1,
        )
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
        page.wait_for_selector("#tierCards .card")
        page.evaluate(f"document.documentElement.style.zoom = '{PAGE_ZOOM}'")
        page.wait_for_timeout(400)

        # 1. the three numbers
        beat(7.0, page)

        # 2. the exception ledger
        page.click("#tab-exceptions")
        page.wait_for_selector("#excBody tr")
        beat(7.0, page)

        # 3. isolate one reason code. Located by its label so the shot does not depend
        #    on button order.
        filters = page.query_selector_all("#reasonFilters button")
        for button in filters:
            if "NET_IDENTITY_BREAK" in (button.inner_text() or ""):
                button.click()
                break
        beat(6.0, page)

        # 4. the arithmetic behind one settlement
        page.click("#tab-records")
        page.fill("#recSearch", "setl_00016")
        page.wait_for_selector("#recResults .detail")
        beat(3.0, page)
        page.mouse.wheel(0, 260)
        beat(8.0, page)

        # 5. a live, grounded answer
        page.click("#tab-ask")
        page.wait_for_selector("#chatInput")
        beat(2.0, page)
        page.fill("#chatInput", "Which proposals did the verification gate reject?")
        beat(1.5, page)
        page.click("#chatSend")
        page.wait_for_selector(".msg.bot .meta", timeout=20_000)
        beat(9.0, page)

        context.close()
        browser.close()

    videos = sorted(out_dir.glob("*.webm"), key=lambda p: p.stat().st_mtime)
    if not videos:
        raise SystemExit("playwright produced no video")
    return videos[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="media/browser.mp4")
    parser.add_argument("--port", type=int, default=8433)
    parser.add_argument("--pace", type=float, default=1.0)
    parser.add_argument("--crf", type=int, default=20)
    args = parser.parse_args(argv)

    ffmpeg = find_ffmpeg()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw_dir = out.parent / "_browser_raw"
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    raw_dir.mkdir(parents=True)

    print(f"starting recon.serve on port {args.port}...")
    server = subprocess.Popen(
        [sys.executable, "-m", "recon.serve", "--no-browser", "--port", str(args.port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_server(args.port)
        print("server up, recording browser...")
        webm = record(args.port, raw_dir, args.pace)
        print(f"raw capture: {webm.name}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    print("transcoding to h264...")
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(webm),
         "-vf", "fps=24,scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black",
         "-an",
         "-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf),
         "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
         "-movflags", "+faststart", str(out)],
        check=True,
    )
    shutil.rmtree(raw_dir, ignore_errors=True)

    check = subprocess.run([ffmpeg, "-v", "error", "-i", str(out), "-f", "null", "-"],
                           capture_output=True, text=True)
    if check.stderr.strip():
        print(check.stderr.strip()[:600])
        raise SystemExit("browser capture is not a clean bitstream")

    size_mb = out.stat().st_size / 1_048_576
    print(f"wrote {out} ({size_mb:.1f} MB), decoded clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
