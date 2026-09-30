"""Stand-in for hc-ping.com: records every ping; GET /_pings lists them."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PINGS = []


class H(BaseHTTPRequestHandler):
    def _record(self):
        n = int(self.headers.get("Content-Length") or 0)
        PINGS.append({"method": self.command, "path": self.path, "body": self.rfile.read(n).decode()})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        if self.path == "/_pings":
            body = json.dumps(PINGS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self._record()

    do_POST = _record


ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
