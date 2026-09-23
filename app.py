#!/usr/bin/env python3
"""Launch ForgeCycle's local graphical interface in the default browser."""
from __future__ import annotations

import json
import mimetypes
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from forgecycle.engine import RUNS_DIR, Workflow


STATIC = Path(__file__).resolve().parent / "static"
workflow = Workflow()
csrf = secrets.token_urlsafe(32)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # HTTP paths can contain credentials in malicious requests; never log them.
        pass

    def send_json(self, value, code=200):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            self.send_json({**workflow.public(), "csrf": csrf})
            return
        if path == "/api/history":
            items = []
            for file in sorted(RUNS_DIR.glob("*.json"), reverse=True)[:25]:
                try:
                    item = json.loads(file.read_text(encoding="utf-8"))
                    items.append({k: item.get(k) for k in ("id", "prompt", "phase", "workspace", "round")})
                except (OSError, ValueError):
                    pass
            self.send_json({"history": items})
            return
        if path.startswith("/api/history/"):
            name = path.rsplit("/", 1)[-1]
            if not name.isdigit():
                self.send_json({"error": "غير موجود"}, 404)
                return
            file = RUNS_DIR / (name + ".json")
            try:
                self.send_json({"state": json.loads(file.read_text(encoding="utf-8"))})
            except (OSError, ValueError):
                self.send_json({"error": "غير موجود"}, 404)
            return
        name = "index.html" if path == "/" else path.lstrip("/")
        file = (STATIC / name).resolve()
        if not file.is_relative_to(STATIC) or not file.is_file():
            self.send_error(404)
            return
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(file)[0] or "application/octet-stream") + ("; charset=utf-8" if file.suffix in (".html", ".css", ".js") else ""))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        hostname = host.split(":", 1)[0]
        allowed = {f"http://{host}"} if hostname in ("127.0.0.1", "localhost") else set()
        if origin not in allowed or self.headers.get("X-ForgeCycle-Token") != csrf:
            self.send_json({"error": "طلب غير مسموح."}, 403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 70_000:
                raise ValueError("حجم الطلب كبير.")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("طلب غير صالح.")
            path = urlparse(self.path).path
            if path == "/api/settings":
                result = {"settings": workflow.save_settings(body)}
            elif path == "/api/models":
                result = workflow.models(body.get("role", ""), body)
            elif path == "/api/start":
                workflow.start(str(body.get("prompt", "")), body.get("max_rounds", 4), body.get("max_calls", 60))
                result = {"ok": True}
            elif path == "/api/answer":
                workflow.answer(body.get("answers", []))
                result = {"ok": True}
            elif path == "/api/approve":
                workflow.approve(str(body.get("brief", "")), body.get("acceptance", []), str(body.get("workspace", "")), str(body.get("test_command", "")))
                result = {"ok": True}
            elif path == "/api/cancel":
                workflow.cancel()
                result = {"ok": True}
            else:
                self.send_json({"error": "غير موجود"}, 404)
                return
            self.send_json(result)
        except (ValueError, RuntimeError) as exc:
            self.send_json({"error": str(exc)[:500]}, 400)
        except Exception:
            self.send_json({"error": "حدث خطأ داخلي. راجع السجل المحلي."}, 500)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"ForgeCycle ready: {url}", flush=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nForgeCycle stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
