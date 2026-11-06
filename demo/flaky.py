#!/usr/bin/env python3
"""A service that misbehaves on purpose, for the demo and for testing the alert path.

Cycles healthy, slow, failing. --fast brings the cycle down to 20s.

The slow mode sleeps 300-600ms on purpose: under the 700ms probe timeout, over the 250ms
latency objective. Requests succeed, just slowly, which is the case a status-code-only
monitor never sees.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODES = ("healthy", "slow", "failing")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "healthy"
    started = time.time()
    cycle = 60.0

    def log_message(self, *args):
        return

    def do_GET(self):  # noqa: N802
        # A prober that gives up at its timeout closes the socket mid-response. That is
        # the prober working correctly, not an error worth a traceback per request.
        try:
            self._respond()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _respond(self):
        elapsed = time.time() - Handler.started
        if Handler.mode is None or Handler.mode == "cycle":
            index = int(elapsed // Handler.cycle) % len(MODES)
            mode = MODES[index]
        else:
            mode = Handler.mode

        if mode == "slow":
            time.sleep(random.uniform(0.30, 0.60))
        if mode == "failing" and random.random() < 0.7:
            body = json.dumps({"status": "unavailable", "mode": mode}).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = json.dumps({"status": "ok", "mode": mode, "uptime": round(elapsed, 1)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="a service that fails on a schedule")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--mode", default="cycle", choices=MODES + ("cycle",))
    parser.add_argument("--fast", action="store_true", help="20s cycle instead of 60s")
    args = parser.parse_args()
    Handler.mode = args.mode
    Handler.cycle = 20.0 if args.fast else 60.0
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"flaky demo service on :{args.port}, mode={args.mode}, cycle={Handler.cycle:.0f}s",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
