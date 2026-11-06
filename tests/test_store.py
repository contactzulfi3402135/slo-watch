import pytest

from slo_watch.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_availability_counts_respect_the_window(store):
    store.record("api", ts=100.0, ok=True, latency_ms=10)
    store.record("api", ts=200.0, ok=False, latency_ms=10)
    store.record("api", ts=900.0, ok=True, latency_ms=10)
    counts = store.availability_counts("api", since_ts=150.0)
    assert (counts.good, counts.total) == (1, 2)
    assert counts.bad == 1


def test_latency_counts_only_count_successful_probes(store):
    store.record("api", ts=100.0, ok=True, latency_ms=10)
    store.record("api", ts=101.0, ok=True, latency_ms=900)
    store.record("api", ts=102.0, ok=False, latency_ms=5)
    counts = store.latency_counts("api", since_ts=0, threshold_ms=250)
    # The failed probe is excluded entirely: it has no latency to judge.
    assert (counts.good, counts.total) == (1, 2)


def test_services_do_not_leak_into_each_other(store):
    store.record("api", 100.0, True, 10)
    store.record("web", 100.0, False, 10)
    assert store.availability_counts("api", 0).total == 1
    assert store.availability_counts("web", 0).bad == 1


def test_prune_removes_only_old_rows(store):
    store.record("api", 100.0, True, 10)
    store.record("api", 5000.0, True, 10)
    removed = store.prune(older_than_ts=1000.0)
    assert removed == 1
    assert store.availability_counts("api", 0).total == 1


def test_alert_lifecycle_persists_and_counts_notifications(store):
    first = store.upsert_alert(fingerprint="api/availability/fast", service="api", slo="availability",
                               rule="fast", severity="page", state="firing", now=1000.0,
                               burn_long=20.0, burn_short=30.0, notified=True)
    assert first.notifications == 1 and first.resolved_at is None

    second = store.upsert_alert(fingerprint="api/availability/fast", service="api", slo="availability",
                                rule="fast", severity="page", state="firing", now=4600.0,
                                burn_long=22.0, burn_short=25.0, notified=True)
    assert second.notifications == 2
    assert store.active_alerts()[0].fingerprint == "api/availability/fast"

    resolved = store.upsert_alert(fingerprint="api/availability/fast", service="api", slo="availability",
                                  rule="fast", severity="page", state="resolved", now=9000.0,
                                  burn_long=0.2, burn_short=0.1, notified=True)
    assert resolved.state == "resolved" and resolved.resolved_at == 9000.0
    assert store.active_alerts() == []
    assert len(store.alert_history()) == 1


def test_series_buckets_by_minute(store):
    for i in range(120):
        store.record("api", ts=1000.0 + i, ok=True, latency_ms=20)
    # 120 rows starting at 1000s land in three minute buckets: 960, 1020, 1080.
    series = store.series("api", since_ts=0, bucket_seconds=60)
    assert len(series) == 3
    assert [total for _, _, total, _ in series] == [20, 60, 40]
    assert sum(total for _, _, total, _ in series) == 120


def test_refiring_clears_the_previous_resolve(store):
    store.upsert_alert(fingerprint="api/latency/fast", service="api", slo="latency", rule="fast",
                       severity="page", state="firing", now=100.0, burn_long=9.0, burn_short=9.0,
                       notified=True)
    store.upsert_alert(fingerprint="api/latency/fast", service="api", slo="latency", rule="fast",
                       severity="page", state="resolved", now=200.0, burn_long=0.5, burn_short=0.5,
                       notified=True)
    again = store.upsert_alert(fingerprint="api/latency/fast", service="api", slo="latency",
                               rule="fast", severity="page", state="firing", now=300.0,
                               burn_long=12.0, burn_short=15.0, notified=True)
    assert again.state == "firing"
    assert again.resolved_at is None
    assert [a.fingerprint for a in store.active_alerts()] == ["api/latency/fast"]
