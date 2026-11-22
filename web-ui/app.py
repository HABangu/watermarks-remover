from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import json

ROOT = Path(__file__).resolve().parent
API = 'http://127.0.0.1:8765'

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        if self.path == '/api/health':
            self.proxy('GET', '/health')
        else:
            super().do_GET()

    def do_POST(self):
        if self.path not in ('/api/inspect', '/api/clean'):
            self.send_error(404)
            return
        self.proxy('POST', self.path[4:])

    def proxy(self, method, endpoint):
        length = int(self.headers.get('Content-Length', '0'))
        if length > 18 * 1024 * 1024:
            self.send_error(413, 'Request too large')
            return
        body = self.rfile.read(length) if method == 'POST' else None
        req = Request(API + endpoint, data=body, method=method,
                      headers={'Content-Type': 'application/json'} if body else {})
        try:
            with urlopen(req, timeout=180) as response:
                payload = response.read()
                self.send_response(response.status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(payload)
        except HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (URLError, TimeoutError) as e:
            payload = json.dumps({'error': 'Local processing service is unavailable: ' + str(e)}).encode()
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, fmt, *args):
        print('%s - %s' % (self.address_string(), fmt % args))

if __name__ == '__main__':
    print('Watermarks Remover UI: http://127.0.0.1:8766')
    ThreadingHTTPServer(('127.0.0.1', 8766), Handler).serve_forever()
