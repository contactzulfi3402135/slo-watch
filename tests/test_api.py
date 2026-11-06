import time

import pytest

from slo_watch.api import create_app
from slo_watch.config import parse_config
from slo_watch.monitor import Monitor
from slo_watch.store import Store

RAW = {
    "interval_seconds": 5,
    "services": [{
        "name": "api",
        "probes": [{"type": "command", "command": "true"}],
        "slos": [
            {"name": "availability", "sli": "availability", "objective": 99.0, "window": "28d", "min_events": 1},
            {"name": "latency", "sli": "latency", "objective": 95.0, "window": "28d",
             "latency_threshold_ms": 250, "min_events": 1},
        ],
    }],
    "rules": [{"name": "fast", "long_window": "5m", "short_window": "1m",
               "burn_rate": 5, "severity": "page", "min_events": 4}],
    "notifiers": [{"type": "log"}],
}


@pytest.fixture()
def client(tmp_path):
    config = parse_config({**RAW, "database": str(tmp_path / "api.db")})
    store = Store(config.database)
    monitor = Monitor(config, store)
    app = create_app(config, monitor)
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c, monitor, store
    store.close()


def test_dashboard_renders(client):
    c, _, _ = client
    body = c.get("/").get_data(as_text=True)
    assert "slo-watch" in body
    assert "/api/summary" in body


def test_summary_reports_slos_and_last_probe(client):
    c, monitor, store = client
    now = time.time()
    store.record("api", now, True, 12.0)
    store.record("api", now, False, 9.0)
    body = c.get("/api/summary").get_json()
    assert [s["name"] for s in body["services"]] == ["api"]
    availability = body["services"][0]["slos"][0]
    assert availability["total"] == 2 and availability["good"] == 1
    assert availability["compliance"] == pytest.approx(50.0)
    assert body["services"][0]["last_probe"]["ok"] in (True, False)
    assert {r["name"] for r in body["rules"]} == {"fast"}


def test_unknown_service_is_404_not_500(client):
    c, _, _ = client
    response = c.get("/api/services/nope/recent")
    assert response.status_code == 404
    assert "unknown service" in response.get_json()["message"]


def test_manual_probe_records_a_row(client):
    c, _, store = client
    response = c.post("/api/services/api/probe")
    assert response.status_code == 202
    assert response.get_json()["results"][0]["ok"] is True
    assert store.availability_counts("api", 0).total == 1


def test_recent_probes_are_newest_first(client):
    c, _, store = client
    now = time.time()
    store.record("api", now - 10, True, 5.0)
    store.record("api", now, False, 5.0)
    rows = c.get("/api/services/api/recent").get_json()
    assert [r["ok"] for r in rows] == [False, True]


def test_metrics_are_prometheus_text(client):
    c, _, store = client
    store.record("api", time.time(), True, 11.0)
    text = c.get("/metrics").get_data(as_text=True)
    assert 'slo_compliance_ratio{service="api",slo="availability"' in text
    assert "slo_valid_events_total" in text
    assert text.rstrip().endswith("slo_alerts_firing 0")


def test_alert_history_includes_resolved_alerts(client):
    c, _, store = client
    store.upsert_alert(fingerprint="api/availability/fast", service="api", slo="availability",
                       rule="fast", severity="page", state="resolved", now=time.time(),
                       burn_long=0.1, burn_short=0.0, notified=True)
    history = c.get("/api/alerts").get_json()
    assert history[0]["state"] == "resolved"


def test_healthz_is_liveness_only(client):
    c, _, store = client
    store.record("api", time.time(), False, 1.0)     # an unhealthy service
    body = c.get("/healthz").get_json()
    assert body["status"] == "ok"
    assert body["services"] == 1
