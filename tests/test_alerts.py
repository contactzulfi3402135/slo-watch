import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from slo_watch.alerts import Evaluator, LogNotifier, RecordingNotifier, WebhookNotifier, fingerprint
from slo_watch.config import AlertRule, SLO, Service
from slo_watch.store import Store

AVAILABILITY = SLO(name="availability", objective=99.0, window_seconds=28 * 86400,
                   sli="availability", min_events=1)
SERVICE = Service(name="api", probes=(), slos=(AVAILABILITY,))
FAST = AlertRule(name="fast", long_window=300, short_window=60, burn_rate=5,
                 severity="page", renotify_seconds=600, min_events=1)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "alerts.db")
    yield s
    s.close()


@pytest.fixture()
def recorder():
    return RecordingNotifier()


@pytest.fixture()
def evaluator(store, recorder):
    return Evaluator(store, [recorder])


def feed(store, now, total, bad, span=300):
    """Records `total` probes ending at `now`; the most recent `bad` of them failed."""
    step = span / total
    for i in range(total):
        store.record("api", now - span + i * step, i < total - bad, 10.0)


NOW = 1_000_000.0


def test_fires_when_both_windows_exceed(evaluator, store, recorder):
    feed(store, NOW, total=100, bad=20)          # 20% bad against a 1% budget: burn 20x
    decisions = evaluator.tick((SERVICE,), (FAST,), NOW)
    assert [d.state for d in decisions] == ["firing"]
    assert decisions[0].burn_long == pytest.approx(20.0)
    assert len(recorder.sent) == 1
    assert store.active_alerts()[0].fingerprint == "api/availability/fast"


def test_does_not_fire_when_only_the_long_window_exceeds(evaluator, store, recorder):
    # Bad probes across the long window, nothing bad in the last minute: the short window
    # is what stops a stale incident from paging again.
    for i in range(40):
        store.record("api", NOW - 240 + i, i < 10, 10.0)
    for i in range(40):
        store.record("api", NOW - 40 + i * 0.5, True, 10.0)
    decisions = evaluator.tick((SERVICE,), (FAST,), NOW)
    assert decisions == []
    assert recorder.sent == []


def test_resolves_when_the_long_window_recovers(evaluator, store, recorder):
    feed(store, NOW, total=100, bad=20)
    evaluator.tick((SERVICE,), (FAST,), NOW)
    later = NOW + 600
    feed(store, later, total=100, bad=0)
    decisions = evaluator.tick((SERVICE,), (FAST,), later)
    assert [d.state for d in decisions] == ["resolved"]
    assert decisions[0].burn_long == pytest.approx(0.0)
    assert [d.state for d, _ in recorder.sent] == ["firing", "resolved"]
    assert store.active_alerts() == []


def test_resolves_on_short_window_recovery_while_the_long_window_is_still_hot(evaluator, store, recorder):
    """Dual-window is mostly about recovery speed: the long window still remembers the
    outage, and the alert should not keep paging because of it."""
    feed(store, NOW, total=100, bad=20)
    evaluator.tick((SERVICE,), (FAST,), NOW)
    assert [d.state for d, _ in recorder.sent] == ["firing"]

    quiet = NOW + 90                                  # a clean minute, after the outage
    for i in range(70):
        store.record("api", quiet - 60 + i * 0.85, True, 10.0)
    decisions = evaluator.tick((SERVICE,), (FAST,), quiet)
    assert [d.state for d in decisions] == ["resolved"]
    assert decisions[0].burn_long >= FAST.burn_rate    # the long window has not forgotten
    assert decisions[0].burn_short < FAST.burn_rate


def test_holds_state_when_the_long_window_has_no_data(evaluator, store):
    feed(store, NOW, total=100, bad=20)
    evaluator.tick((SERVICE,), (FAST,), NOW)
    # Nothing recorded for an hour: the service is silent, not healthy.
    assert evaluator.tick((SERVICE,), (FAST,), NOW + 3600) == []
    assert [a.state for a in store.active_alerts()] == ["firing"]


def test_holds_state_when_the_short_window_has_no_data(store):
    # A rule that needs 20 samples per window, so "no data" is easy to arrange honestly.
    gated = AlertRule(name="fast", long_window=300, short_window=60, burn_rate=5,
                      severity="page", renotify_seconds=600, min_events=20)
    recorder = RecordingNotifier()
    evaluator = Evaluator(store, [recorder])

    for i in range(60):                                     # 10 bad spread over the long window
        store.record("api", NOW - 300 + i * 5, i < 10, 10.0)
    assert evaluator.tick((SERVICE,), (gated,), NOW) == []   # short window is empty
    assert recorder.sent == []

    for i in range(30):                                     # a burst: both windows now exceed
        store.record("api", NOW - 30 + i, i < 25, 10.0)
    firing = evaluator.tick((SERVICE,), (gated,), NOW)
    assert [d.state for d in firing] == ["firing"]
    assert len(recorder.sent) == 1

    # The short window empties out while the long window is still above the threshold: the
    # alert must keep firing, and must not notify again.
    thin = NOW + 100
    for i in range(3):
        store.record("api", thin - i, True, 10.0)
    held = evaluator.tick((SERVICE,), (gated,), thin)
    assert [d.state for d in held] == ["firing"]
    assert held[0].burn_short is None and held[0].notify is False
    assert [a.state for a in store.active_alerts()] == ["firing"]
    assert len(recorder.sent) == 1


def test_renotifies_only_after_the_interval(evaluator, store, recorder):
    feed(store, NOW, total=100, bad=20)
    evaluator.tick((SERVICE,), (FAST,), NOW)
    assert len(recorder.sent) == 1

    feed(store, NOW + 60, total=100, bad=20)                 # still burning, 1 minute later
    decisions = evaluator.tick((SERVICE,), (FAST,), NOW + 60)
    assert decisions[0].state == "firing"
    assert decisions[0].notify is False
    assert len(recorder.sent) == 1

    feed(store, NOW + 900, total=200, bad=40)                # past the 10 minute renotify
    decisions = evaluator.tick((SERVICE,), (FAST,), NOW + 900)
    assert decisions[0].notify is True
    assert len(recorder.sent) == 2
    assert store.get_alert("api/availability/fast").notifications == 2


def test_min_events_is_not_ignored_until_there_is_data(evaluator, store, recorder):
    store.record("api", NOW - 1, False, 10.0)
    store.record("api", NOW, False, 10.0)
    rule = AlertRule(name="fast", long_window=300, short_window=60, burn_rate=5,
                     severity="page", min_events=20)
    assert evaluator.tick((SERVICE,), (rule,), NOW) == []
    assert recorder.sent == []


def test_fingerprint_is_stable_and_readable():
    assert fingerprint("api", "availability", "fast") == "api/availability/fast"


def _alert(store, **overrides):
    payload = {"fingerprint": "api/availability/fast", "service": "api", "slo": "availability",
               "rule": "fast", "severity": "page", "state": "firing", "now": time.time(),
               "burn_long": 20.0, "burn_short": 20.0, "notified": True}
    payload.update(overrides)
    return store.upsert_alert(**payload)


def test_log_notifier_writes_one_line(store, caplog):
    from slo_watch.alerts import Decision
    decision = Decision("api/availability/fast", "api", "availability", "fast", "page",
                        "firing", 20.0, 20.0, True, "test")
    with caplog.at_level("WARNING", logger="slo_watch.alerts"):
        LogNotifier().send(decision, _alert(store))
    assert "ALERT FIRING api/availability fast" in caplog.text
    assert "burn(long)=20.00x" in caplog.text


class _Collector(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received: list[dict] = []

    def log_message(self, *args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers["Content-Length"])
        _Collector.received.append(json.loads(self.rfile.read(length)))
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture()
def webhook_server():
    _Collector.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def test_webhook_posts_the_alert(webhook_server, store):
    from slo_watch.alerts import Decision
    url = f"http://127.0.0.1:{webhook_server.server_address[1]}/hook"
    notifier = WebhookNotifier(url, timeout_ms=2000)
    alert = _alert(store)
    decision = Decision("api/availability/fast", "api", "availability", "fast", "page",
                        "firing", 20.0, 20.0, True, "test")
    notifier.send(decision, alert)
    assert _Collector.received[0]["state"] == "firing"
    assert _Collector.received[0]["service"] == "api"
    assert _Collector.received[0]["burn_rate_long"] == 20.0


def test_webhook_failure_is_not_fatal(store):
    from slo_watch.alerts import Decision
    notifier = WebhookNotifier("http://127.0.0.1:9/nothing-listens-here", timeout_ms=200)
    decision = Decision("api/availability/fast", "api", "availability", "fast", "page",
                        "firing", 1.0, 1.0, True, "test")
    notifier.send(decision, _alert(store))     # must not raise
