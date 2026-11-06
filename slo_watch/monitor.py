"""Glue between config, probes, storage and alerting. The CLI and the HTTP API both call
this, so a probe from the dashboard takes the same path as a scheduled one."""

from __future__ import annotations

import time
from dataclasses import asdict

from .alerts import Decision, Evaluator, slo_report
from .config import Config, SLO, Service
from .probe import ProbeResult, run_probe
from .sli import time_to_exhaustion_hours
from .store import Counts, Store


class Monitor:
    def __init__(self, config: Config, store: Store, evaluator: Evaluator | None = None):
        self.config = config
        self.store = store
        self.evaluator = evaluator or Evaluator(store)

    # ------------------------------------------------------------------ probing

    def probe_service(self, service: Service, now: float | None = None) -> list[ProbeResult]:
        ts = time.time() if now is None else now
        results: list[ProbeResult] = []
        for probe in service.probes:
            result = run_probe(service.name, probe, now=lambda: ts)
            # failures get stored too, or availability reads 100% during an outage
            self.store.record(service.name, result.ts, result.ok, result.latency_ms, result.detail)
            results.append(result)
        return results

    def probe_all(self, now: float | None = None) -> list[ProbeResult]:
        out: list[ProbeResult] = []
        for service in self.config.services:
            out.extend(self.probe_service(service, now=now))
        return out

    # ------------------------------------------------------------------ evaluating

    def evaluate(self, now: float | None = None) -> list[Decision]:
        ts = time.time() if now is None else now
        return self.evaluator.tick(self.config.services, self.config.rules, ts)

    def prune(self, now: float | None = None) -> int:
        ts = time.time() if now is None else now
        return self.store.prune(ts - self.config.retention_seconds)

    # ------------------------------------------------------------------ reporting

    def slo_table(self, now: float | None = None) -> list[dict]:
        ts = time.time() if now is None else now
        rows: list[dict] = []
        for service in self.config.services:
            for slo in service.slos:
                row = slo_report(self.store, service, slo, ts).as_dict()
                exhaustion = time_to_exhaustion_hours(
                    self._counts(service, slo, ts - slo.window_seconds),
                    slo.objective, slo.window_seconds, slo.min_events,
                )
                row["exhausted_in_hours"] = None if exhaustion is None else round(exhaustion, 2)
                row["service"] = service.name
                rows.append(row)
        return rows

    def summary(self, now: float | None = None) -> dict:
        ts = time.time() if now is None else now
        services = []
        for service in self.config.services:
            recent = self.store.recent(service.name, limit=1)
            last = recent[0] if recent else None
            services.append({
                "name": service.name,
                "probes": [asdict(p) for p in service.probes],
                "slos": [r for r in self.slo_table(ts) if r["service"] == service.name],
                "last_probe": None if last is None else {
                    "ts": last["ts"], "ok": bool(last["ok"]),
                    "latency_ms": round(last["latency_ms"], 2), "detail": last["detail"],
                },
                "series": [
                    {"ts": b, "good": good, "total": total, "latency_ms": round(ms, 1)}
                    for b, good, total, ms in self.store.series(service.name, ts - 3600, 60)
                ],
            })
        return {
            "generated_at": ts,
            "interval_seconds": self.config.interval_seconds,
            "services": services,
            "alerts": [asdict(a) for a in self.store.active_alerts()],
            "rules": [
                {"name": r.name, "long_window": r.long_window, "short_window": r.short_window,
                 "burn_rate": r.burn_rate, "severity": r.severity}
                for r in self.config.rules
            ],
        }

    def _counts(self, service: Service, slo: SLO, since_ts: float) -> Counts:
        if slo.sli == "latency":
            return self.store.latency_counts(service.name, since_ts, slo.latency_threshold_ms)
        return self.store.availability_counts(service.name, since_ts)
