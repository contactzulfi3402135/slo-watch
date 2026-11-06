"""SQLite: raw probe rows plus alert state.

Raw rather than pre-aggregated, so an SLI query is a filtered COUNT on an index and the
report can ask questions the schema wasn't built for. Retention is the price of that:
prune() runs on a timer, and a long window would need rollups that aren't here.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS probe_results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    service    TEXT    NOT NULL,
    ts         REAL    NOT NULL,
    ok         INTEGER NOT NULL,
    latency_ms REAL    NOT NULL,
    detail     TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS alerts (
    fingerprint   TEXT PRIMARY KEY,
    service       TEXT    NOT NULL,
    slo           TEXT    NOT NULL,
    rule          TEXT    NOT NULL,
    severity      TEXT    NOT NULL,
    state         TEXT    NOT NULL,
    started_at    REAL    NOT NULL,
    resolved_at   REAL,
    last_notified REAL,
    notifications INTEGER NOT NULL DEFAULT 0,
    burn_long     REAL,
    burn_short    REAL
);
CREATE INDEX IF NOT EXISTS idx_alerts_state ON alerts(state);
CREATE INDEX IF NOT EXISTS idx_probe_service_ts ON probe_results(service, ts);
"""


@dataclass(frozen=True)
class Counts:
    good: int
    total: int

    @property
    def bad(self) -> int:
        return self.total - self.good


@dataclass(frozen=True)
class AlertRow:
    fingerprint: str
    service: str
    slo: str
    rule: str
    severity: str
    state: str
    started_at: float
    resolved_at: float | None
    last_notified: float | None
    notifications: int
    burn_long: float | None
    burn_short: float | None


class Store:
    """Thread-safe wrapper around one SQLite file.

    The Flask app and the scheduler run in different threads, so the connection is
    created with check_same_thread=False and every write goes through a lock. One writer
    and many readers is exactly SQLite's happy path.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._tx() as cur:
            cur.executescript(SCHEMA)
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")

    @contextmanager
    def _tx(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- writes

    def record(self, service: str, ts: float, ok: bool, latency_ms: float, detail: str = "") -> None:
        with self._lock, self._tx() as cur:
            cur.execute(
                "INSERT INTO probe_results (service, ts, ok, latency_ms, detail) VALUES (?,?,?,?,?)",
                (service, ts, 1 if ok else 0, latency_ms, detail[:500]),
            )

    def record_many(self, rows: list[tuple[str, float, int, float]]) -> None:
        if not rows:
            return
        with self._lock, self._tx() as cur:
            cur.executemany(
                "INSERT INTO probe_results (service, ts, ok, latency_ms) VALUES (?,?,?,?)", rows
            )

    def prune(self, older_than_ts: float) -> int:
        with self._lock, self._tx() as cur:
            cur.execute("DELETE FROM probe_results WHERE ts < ?", (older_than_ts,))
            return cur.rowcount

    # ---------------------------------------------------------------- reads

    def availability_counts(self, service: str, since_ts: float) -> Counts:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(ok),0) AS good "
                "FROM probe_results WHERE service = ? AND ts >= ?",
                (service, since_ts),
            ).fetchone()
        return Counts(good=int(row["good"]), total=int(row["total"]))

    def latency_counts(self, service: str, since_ts: float, threshold_ms: float) -> Counts:
        """Latency SLI: only successful probes are valid events.

        A request that failed never had a latency to judge. Counting it as fast would
        hide an outage behind the latency SLO; counting it as slow would report the
        same failure against two objectives.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN latency_ms <= ? THEN 1 ELSE 0 END),0) AS good "
                "FROM probe_results WHERE service = ? AND ts >= ? AND ok = 1",
                (threshold_ms, service, since_ts),
            ).fetchone()
        return Counts(good=int(row["good"]), total=int(row["total"]))

    def recent(self, service: str, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT ts, ok, latency_ms, detail FROM probe_results "
                "WHERE service = ? ORDER BY ts DESC LIMIT ?",
                (service, limit),
            ).fetchall()

    def series(self, service: str, since_ts: float, bucket_seconds: int) -> list[tuple[float, int, int, float]]:
        """Bucketed series for the dashboard sparkline: (bucket_ts, good, total, avg_ms)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, "
                "       SUM(ok) AS good, COUNT(*) AS total, AVG(latency_ms) AS avg_ms "
                "FROM probe_results WHERE service = ? AND ts >= ? "
                "GROUP BY bucket ORDER BY bucket",
                (bucket_seconds, bucket_seconds, service, since_ts),
            ).fetchall()
        return [(float(r["bucket"]), int(r["good"]), int(r["total"]), float(r["avg_ms"] or 0)) for r in rows]

    # ---------------------------------------------------------------- alerts

    def upsert_alert(self, *, fingerprint: str, service: str, slo: str, rule: str, severity: str,
                     state: str, now: float, burn_long: float | None, burn_short: float | None,
                     notified: bool) -> AlertRow:
        with self._lock, self._tx() as cur:
            existing = cur.execute("SELECT * FROM alerts WHERE fingerprint = ?", (fingerprint,)).fetchone()
            if existing is None:
                cur.execute(
                    "INSERT INTO alerts (fingerprint, service, slo, rule, severity, state, started_at, "
                    "resolved_at, last_notified, notifications, burn_long, burn_short) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fingerprint, service, slo, rule, severity, state, now,
                     None, now if notified else None, 1 if notified else 0, burn_long, burn_short),
                )
            else:
                notifications = int(existing["notifications"]) + (1 if notified else 0)
                cur.execute(
                    "UPDATE alerts SET state = ?, burn_long = ?, burn_short = ?, "
                    "resolved_at = ?, last_notified = ?, notifications = ? WHERE fingerprint = ?",
                    # re-firing clears resolved_at, otherwise a fresh incident keeps the
                    # timestamp of the one it already survived
                    (state, burn_long, burn_short, now if state == "resolved" else None,
                     now if notified else existing["last_notified"], notifications, fingerprint),
                )
            row = cur.execute("SELECT * FROM alerts WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return _alert_row(row)

    def active_alerts(self) -> list[AlertRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts WHERE state = 'firing' ORDER BY started_at DESC"
            ).fetchall()
        return [_alert_row(r) for r in rows]

    def alert_history(self, limit: int = 50) -> list[AlertRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_alert_row(r) for r in rows]

    def get_alert(self, fingerprint: str) -> AlertRow | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM alerts WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return _alert_row(row) if row else None


def _alert_row(row: sqlite3.Row) -> AlertRow:
    return AlertRow(
        fingerprint=row["fingerprint"], service=row["service"], slo=row["slo"], rule=row["rule"],
        severity=row["severity"], state=row["state"], started_at=row["started_at"],
        resolved_at=row["resolved_at"], last_notified=row["last_notified"],
        notifications=int(row["notifications"]), burn_long=row["burn_long"], burn_short=row["burn_short"],
    )
