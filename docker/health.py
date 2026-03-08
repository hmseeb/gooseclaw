"""minimal health check server for railway.

railway requires an HTTP endpoint that returns 200 to confirm
the deployment is healthy. this tiny server does that and nothing else.
"""
import http.server
import json
import os

PORT = int(os.environ.get("PORT", 8080))


class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/health", "/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            body = {
                "status": "healthy",
                "agent": "nix",
                "version": "1.0.0",
            }
            self.wfile.write(json.dumps(body).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress request logs


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"[health] listening on 0.0.0.0:{PORT}")
    server.serve_forever()
