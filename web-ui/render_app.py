#!/usr/bin/env python3
"""Single-container Render entry point: public UI + loopback-only cleaner API."""
from __future__ import annotations

import atexit
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
API = "http://127.0.0.1:8765"
MAX_BODY = 18 * 1024 * 1024
RATE_WINDOW = 15 * 60
RATE_LIMIT = 12
rates: dict[str, deque[float]] = defaultdict(deque)
rate_lock = threading.Lock()
work_slots = threading.BoundedSemaphore(2)

backend = subprocess.Popen(
    [sys.executable, "/app/scripts/server.py", "--host", "127.0.0.1", "--port", "8765"],
    cwd="/app",
    stdout=sys.stdout,
    stderr=sys.stderr,
)
atexit.register(lambda: backend.terminate() if backend.poll() is None else None)

def wait_for_backend() -> None:
    import urllib.request
    for _ in range(60):
        if backend.poll() is not None:
            raise RuntimeError("Processing service exited during startup")
        try:
            with urllib.request.urlopen(API + "/health", timeout=2):
                return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Processing service did not become healthy")

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _client_key(self) -> str:
        # Render supplies X-Forwarded-For at its edge.
        return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()

    def _allow_request(self) -> bool:
        now = time.monotonic()
        key = self._client_key()
        with rate_lock:
            q = rates[key]
            while q and now - q[0] > RATE_WINDOW:
                q.popleft()
            if len(q) >= RATE_LIMIT:
                return False
            q.append(now)
            return True

    def do_GET(self):
        if self.path == "/api/health":
            self._proxy("GET", "/health")
        elif self.path.startswith("/api/"):
            self.send_error(404)
        else:
            super().do_GET()

    def do_POST(self):
        if self.path not in ("/api/inspect", "/api/clean"):
            self.send_error(404)
            return
        if not self._allow_request():
            self._json_error(429, "Request limit reached. Please wait before trying again.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json_error(400, "Invalid Content-Length")
            return
        if length <= 0 or length > MAX_BODY:
            self._json_error(413, "Upload is empty or exceeds the 18 MB limit.")
            return
        if not work_slots.acquire(blocking=False):
            self._json_error(503, "The service is busy. Please try again shortly.")
            return
        try:
            self._proxy("POST", self.path[4:], self.rfile.read(length))
        finally:
            work_slots.release()

    def _json_error(self, status: int, message: str):
        body = ('{"error":' + __import__("json").dumps(message) + '}').encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method: str, endpoint: str, body: bytes | None = None):
        req = Request(API + endpoint, data=body, method=method,
                      headers={"Content-Type": "application/json"} if body else {})
        try:
            with urlopen(req, timeout=180) as response:
                payload = response.read()
                status = response.status
        except HTTPError as exc:
            payload, status = exc.read(), exc.code
        except (URLError, TimeoutError):
            self._json_error(502, "Processing service is temporarily unavailable.")
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

if __name__ == "__main__":
    wait_for_backend()
    port = int(os.environ.get("PORT", "10000"))
    print(f"Watermarks Remover listening on 0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
