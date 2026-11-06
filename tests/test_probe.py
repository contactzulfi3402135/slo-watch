import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from slo_watch.config import Probe
from slo_watch.probe import run_http_probe, run_probe


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def do_GET(self):  # noqa: N802
        if self.path == "/slow":
            time.sleep(0.3)
        status = 500 if self.path == "/boom" else 200
        body = b"ok"
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()


def test_http_probe_reports_ok_and_latency(endpoint):
    result = run_http_probe("api", Probe(type="http", url=f"{endpoint}/health"))
    assert result.ok is True
    assert result.latency_ms >= 0
    assert result.detail == ""


def test_http_probe_fails_on_unexpected_status(endpoint):
    result = run_http_probe("api", Probe(type="http", url=f"{endpoint}/boom"))
    assert result.ok is False
    assert "500" in result.detail


def test_http_probe_times_out_without_raising(endpoint):
    result = run_http_probe("api", Probe(type="http", url=f"{endpoint}/slow", timeout_ms=50))
    assert result.ok is False
    assert "unreachable" in result.detail or "timed out" in result.detail


def test_unreachable_host_is_a_failed_probe_not_an_exception():
    # Port 9 (discard) is closed on this machine.
    result = run_http_probe("api", Probe(type="http", url="http://127.0.0.1:9/health", timeout_ms=200))
    assert result.ok is False
    assert result.detail.startswith("unreachable") or result.detail.startswith("http")


def test_command_probe_uses_the_exit_code():
    ok = run_probe("worker", Probe(type="command", command="exit 0"))
    assert ok.ok is True
    bad = run_probe("worker", Probe(type="command", command="exit 3"))
    assert bad.ok is False and "exit 3" in bad.detail


def test_command_probe_timeout():
    result = run_probe("worker", Probe(type="command", command="sleep 2", timeout_ms=50))
    assert result.ok is False
    assert "timeout" in result.detail
