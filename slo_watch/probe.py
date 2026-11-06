"""HTTP and command probes.

Both return ProbeResult, so storage and the SLI maths don't care where a number came from.
Nothing in here raises: an unreachable host is a failed probe, otherwise the probe thread
dies the first time something goes down. urllib instead of requests to keep the dependency
list at two, and because a timeout here is one number rather than a connect/read tuple.
"""

from __future__ import annotations

import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .config import Probe

USER_AGENT = "slo-watch/1.0 (+https://github.com/zulfiqar-ali/slo-watch)"


@dataclass(frozen=True)
class ProbeResult:
    service: str
    ok: bool
    latency_ms: float
    ts: float
    detail: str = ""

    def as_row(self) -> tuple[str, float, int, float]:
        return (self.service, self.ts, 1 if self.ok else 0, self.latency_ms)


def run_http_probe(service: str, probe: Probe, now: Callable[[], float] = time.time) -> ProbeResult:
    started = time.perf_counter()
    ts = now()
    request = urllib.request.Request(probe.url, method=probe.method,
                                     headers={"User-Agent": USER_AGENT, **probe.headers})
    try:
        with urllib.request.urlopen(request, timeout=probe.timeout_ms / 1000) as response:
            # read a byte so the timer includes the body, not just the headers
            response.read(1)
            latency_ms = (time.perf_counter() - started) * 1000.0
            ok = response.status == probe.expect_status
            detail = "" if ok else f"status {response.status} != {probe.expect_status}"
            return ProbeResult(service, ok, latency_ms, ts, detail)
    except urllib.error.HTTPError as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        return ProbeResult(service, False, latency_ms, ts, f"http {exc.code}")
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        reason = getattr(exc, "reason", exc)
        return ProbeResult(service, False, latency_ms, ts, f"unreachable: {reason}")
    except OSError as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        return ProbeResult(service, False, latency_ms, ts, f"error: {exc}")


def run_command_probe(service: str, probe: Probe, now: Callable[[], float] = time.time) -> ProbeResult:
    started = time.perf_counter()
    ts = now()
    try:
        completed = subprocess.run(
            probe.command, shell=True, timeout=probe.timeout_ms / 1000,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(service, False, (time.perf_counter() - started) * 1000.0, ts,
                           f"timeout after {probe.timeout_ms}ms")
    latency_ms = (time.perf_counter() - started) * 1000.0
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()[:200]
        return ProbeResult(service, False, latency_ms, ts, f"exit {completed.returncode}: {stderr}")
    return ProbeResult(service, True, latency_ms, ts)


def run_probe(service: str, probe: Probe, now: Callable[[], float] = time.time) -> ProbeResult:
    if probe.type == "http":
        return run_http_probe(service, probe, now=now)
    if probe.type == "command":
        return run_command_probe(service, probe, now=now)
    raise ValueError(f"unknown probe type {probe.type!r}")
