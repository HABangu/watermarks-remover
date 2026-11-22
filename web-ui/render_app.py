#!/usr/bin/env python3
"""Single-container Render entry point: public UI + loopback-only cleaner API."""
from __future__ import annotations

import atexit
import base64
import json
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
MAX_IMAGE_PIXELS = 12_000_000
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
        if self.path == "/api/humanize":
            self._humanize()
            return
        if self.path == "/api/inpaint":
            self._inpaint()
            return
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

    def _read_api_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                self._json_error(413, "Request is empty or exceeds the 18 MB limit.")
                return None
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object")
            return value
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._json_error(400, "Invalid JSON request.")
            return None

    def _json_response(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _humanize(self):
        if not self._allow_request():
            self._json_error(429, "Request limit reached. Please wait before trying again.")
            return
        data = self._read_api_json()
        if data is None:
            return
        source = data.get("text")
        if not isinstance(source, str) or not source.strip():
            self._json_error(400, "Enter some text first.")
            return
        if len(source) > 30000:
            self._json_error(413, "Text is too long. Please use 30,000 characters or fewer.")
            return
        try:
            api_key = os.environ.get("GEMINI_API_KEY", "").strip()
            if not api_key:
                self._json_error(503, "Gemini is not configured on the server. Add GEMINI_API_KEY to this Render service's Environment settings, save, and redeploy.")
                return
            style = str(data.get("style", "natural"))[:30].lower()
            style_guidance = {
                "natural": "Use clear, natural, readable prose.",
                "professional": "Use a professional, precise, appropriately formal tone.",
                "plain": "Use plain English, short clear sentences, and familiar words.",
                "conversational": "Use a warm, conversational tone without filler.",
            }.get(style, "Use clear, natural, readable prose.")
            prompt = (
                "Edit the user's text for clarity, grammar, coherence, and readability. Preserve meaning, "
                "facts, qualifications, examples, and the author's own voice. Do not invent evidence, "
                "citations, quotations, data, or references. Keep citations, reference entries, headings, "
                "lists, equations, and technical terms intact. Return only the revised text. This is ordinary "
                "editing, not an attempt to misrepresent authorship or influence AI-detection systems.\n\n"
                f"Style: {style_guidance}\n\nText to edit:\n{source}"
            )
            model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
            payload = json.dumps({
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.35, "maxOutputTokens": 8192},
            }).encode("utf-8")
            request = Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                data=payload, method="POST",
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            )
            with urlopen(request, timeout=90) as response:
                response_data = json.loads(response.read().decode("utf-8"))
            candidates = response_data.get("candidates", [])
            result = ""
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                result = "".join(part.get("text", "") for part in parts if isinstance(part.get("text"), str)).strip()
            if not result:
                self._json_error(502, "Gemini returned no editable text. Check the model response and try again.")
                return
            self._json_response(200, {"ok": True, "text": result, "mode": "gemini-generative-ai",
                "model": model, "note": "Revised with Google Gemini. Review the result to confirm that meaning, facts, and references remain accurate."})
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
            except Exception:
                detail = ""
            message = "Gemini rejected the request. Check that GEMINI_API_KEY is valid, has Gemini API access, and that the selected model is available."
            if detail:
                message += " Provider detail: " + str(detail)[:300]
            self._json_error(502, message)
        except (URLError, TimeoutError):
            self._json_error(504, "Gemini took too long to respond. Please try again.")
        except Exception:
            self._json_error(500, "Text editing is temporarily unavailable.")

    def _inpaint(self):
        if not self._allow_request():
            self._json_error(429, "Request limit reached. Please wait before trying again.")
            return
        data = self._read_api_json()
        if data is None:
            return
        try:
            import cv2
            import numpy as np
            raw = base64.b64decode(data.get("file", ""), validate=True)
            mask_raw = base64.b64decode(data.get("mask", ""), validate=True)
            if len(raw) > 10 * 1024 * 1024:
                raise ValueError("Image exceeds the 10 MB limit.")
            image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
            mask = cv2.imdecode(np.frombuffer(mask_raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if image is None or mask is None:
                raise ValueError("Could not read the image or brush mask.")
            h, w = image.shape[:2]
            if h * w > MAX_IMAGE_PIXELS:
                raise ValueError("Image dimensions exceed 12 megapixels.")
            if mask.shape[:2] != (h, w):
                raise ValueError("Brush mask dimensions do not match the image.")
            if not np.any(mask):
                raise ValueError("Paint over the area you want to repair first.")
            radius = max(2, min(7, int(data.get("radius", 4))))
            repaired = cv2.inpaint(image, mask, radius, cv2.INPAINT_TELEA)
            ok, encoded = cv2.imencode(".png", repaired, [cv2.IMWRITE_PNG_COMPRESSION, 4])
            if not ok:
                raise ValueError("Could not encode repaired image.")
            self._json_response(200, {"ok": True, "name": "repaired-image.png",
                "image": base64.b64encode(encoded.tobytes()).decode("ascii"),
                "method": "OpenCV Telea inpainting",
                "note": "Best for small marks and simple backgrounds. Complex areas may need manual retouching."})
        except ValueError as exc:
            self._json_error(400, str(exc))
        except ImportError:
            self._json_error(503, "Image repair backend is not installed.")
        except Exception:
            self._json_error(500, "Image repair failed. Try a smaller image or a simpler selection.")

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
