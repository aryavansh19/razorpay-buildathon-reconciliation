"""Local dashboard.

    python -m recon.serve

Reconciles a batch once at startup, then serves the HTML report with the question
box enabled. Built on ``http.server`` from the standard library, so there is still
nothing to install.

Scope and safety
----------------
This binds to localhost only and is a read-only view of one in-memory run. There is
no authentication because there is nothing to authorise: every byte it serves is
synthetic data generated locally seconds earlier, the two endpoints cannot mutate
anything, and the socket is not reachable from another machine. Binding to a public
interface would change that calculus, which is why ``--host`` defaults to 127.0.0.1
and warns if you move it.

It is a demo and development surface, not a service. No concurrency, no persistence,
no sessions.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from .agent import ReconciliationView, build_agent
from .classify import HeuristicClassifier
from .generate import GeneratorConfig
from .html_report import render_html
from .models import Tolerance
from .pipeline import generate_and_reconcile

MAX_QUESTION_BYTES = 4_000


class _State:
    """Everything the handler needs, prepared once."""

    def __init__(self, seed: int, days: int, backend: str) -> None:
        self.result = generate_and_reconcile(
            config=GeneratorConfig(seed=seed, days=days),
            tolerance=Tolerance(),
            classifier=HeuristicClassifier(),
        )
        self.view = ReconciliationView(self.result)
        self.agent = build_agent(self.view, backend)
        self.html = render_html(self.result, live=True).encode("utf-8")
        # One question at a time. The agent is stateless per call, but the hosted
        # backend is a network round trip and serialising keeps ordering obvious in
        # a live demo.
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    state: _State
    server_version = "recon-dashboard"

    def log_message(self, fmt: str, *args) -> None:
        # Default logging writes a line per asset request, which buries the useful
        # output during a demo. Only failures are worth surfacing.
        if args and str(args[0]).startswith(("4", "5")):
            sys.stderr.write("  %s\n" % (fmt % args))

    # -- helpers -----------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The page is entirely self-contained, so it never needs to load anything
        # from another origin. Saying so closes off script injection as a class.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, default=str).encode("utf-8"), "application/json")

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, self.state.html, "text/html; charset=utf-8")
        elif path == "/api/report":
            self._send_json(200, self.state.result.report.to_dict())
        elif path == "/api/health":
            self._send_json(200, {"ok": True, "records": self.state.result.report.total_records})
        else:
            self._send_json(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        if self.path.split("?", 1)[0] != "/api/ask":
            self._send_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json(400, {"error": "invalid Content-Length"})
            return
        if length <= 0 or length > MAX_QUESTION_BYTES:
            self._send_json(413, {"error": f"body must be 1 to {MAX_QUESTION_BYTES} bytes"})
            return

        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            question = str(body.get("question", "")).strip()
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": f"could not parse body: {exc}"})
            return
        if not question:
            self._send_json(400, {"error": "question is required"})
            return

        with self.state.lock:
            answer = self.state.agent.ask(question)

        self._send_json(
            200,
            {
                "question": answer.question,
                "text": answer.text,
                "backend": answer.backend,
                "steps": answer.steps,
                "tools": answer.tools_used,
                "cited": answer.citations,
                "ungrounded": answer.ungrounded_citations,
                "grounded": answer.grounded,
                "latency_ms": round(answer.latency_ms, 1),
                "input_tokens": answer.input_tokens,
                "output_tokens": answer.output_tokens,
                "estimated_cost_usd": round(answer.estimated_cost_usd, 6),
                "error": answer.error,
            },
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m recon.serve",
        description="Serve the reconciliation dashboard with the question box enabled.",
    )
    parser.add_argument("--seed", type=int, default=GeneratorConfig.seed)
    parser.add_argument("--days", type=int, default=GeneratorConfig.days)
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Localhost by default; anything else exposes the dashboard.",
    )
    parser.add_argument("--backend", default="auto", choices=("auto", "router", "llm"))
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser.")
    args = parser.parse_args(argv)

    print("Reconciling a batch before serving...")
    state = _State(args.seed, args.days, args.backend)
    report = state.result.report
    print(
        f"  seed {report.seed}: {report.total_records} records, "
        f"{report.auto_matched} auto-matched, {report.assisted_matched} model-assisted, "
        f"{len(report.exceptions)} unresolved, "
        f"{report.records_per_second:.0f} records/second"
    )
    print(f"  agent backend: {getattr(state.agent, 'name', args.backend)} (tools are read-only)")

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"\n  Warning: binding to {args.host} exposes this dashboard on the network. "
            "It has no authentication because it only ever serves locally generated "
            "synthetic data. Do not point it at anything real.\n"
        )

    _Handler.state = state
    httpd = HTTPServer((args.host, args.port), _Handler)
    url = f"http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}/"
    print(f"\n  Dashboard: {url}")
    print("  Endpoints: GET / , GET /api/report , GET /api/health , POST /api/ask")
    print("  Ctrl+C to stop.\n")

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
