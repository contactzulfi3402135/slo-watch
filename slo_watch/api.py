"""Dashboard, JSON reads, Prometheus text, and one endpoint that probes on demand.

Nothing here can change an SLO or silence an alert; those are edits to config.yml, which
means they get reviewed.
"""

from __future__ import annotations

import time

from flask import Flask, Response, abort, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from .alerts import build_notifiers
from .config import Config
from .monitor import Monitor
from .sli import time_to_exhaustion_hours
from .store import Store


def create_app(config: Config, monitor: Monitor | None = None) -> Flask:
    store = Store(config.database)
    monitor = monitor or Monitor(
        config, store, evaluator=None,
    )
    if monitor.store is not store:
        monitor.store = store
    app = Flask(__name__, template_folder="templates")
    app.config["SLO_WATCH"] = {"monitor": monitor, "store": store, "started_at": time.time()}

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html")

    @app.get("/api/summary")
    def summary():
        return jsonify(monitor.summary())

    @app.get("/api/alerts")
    def alerts():
        limit = min(int(request.args.get("limit", 25)), 200)
        return jsonify([
            {
                "fingerprint": a.fingerprint, "service": a.service, "slo": a.slo, "rule": a.rule,
                "severity": a.severity, "state": a.state, "started_at": a.started_at,
                "resolved_at": a.resolved_at, "last_notified": a.last_notified,
                "notifications": a.notifications,
                "burn_rate_long": a.burn_long, "burn_rate_short": a.burn_short,
            }
            for a in store.alert_history(limit)
        ])

    @app.get("/api/services/<name>/recent")
    def recent(name: str):
        service = _service(config, name)
        limit = min(int(request.args.get("limit", 50)), 500)
        rows = store.recent(service.name, limit)
        return jsonify([
            {"ts": r["ts"], "ok": bool(r["ok"]), "latency_ms": round(r["latency_ms"], 2),
             "detail": r["detail"]}
            for r in rows
        ])

    @app.post("/api/services/<name>/probe")
    def manual_probe(name: str):
        """Probe now, on demand. The result is stored like any other, so an impatient
        operator cannot create a second class of data that the SLI maths ignores."""
        service = _service(config, name)
        results = monitor.probe_service(service)
        return jsonify({
            "service": service.name,
            "results": [
                {"ok": r.ok, "latency_ms": round(r.latency_ms, 2), "ts": r.ts, "detail": r.detail}
                for r in results
            ],
        }), 202

    @app.get("/metrics")
    def metrics():
        return Response(_prometheus(config, monitor, time.time()),
                        mimetype="text/plain; version=0.0.4")

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        """Errors under /api answer in JSON: a scraper or a dashboard should not have to
        parse an HTML error page to find out that it used a service name that does not
        exist."""
        if request.path.startswith("/api/"):
            return jsonify({"error": exc.name, "message": exc.description,
                            "status": exc.code}), exc.code
        return exc

    @app.get("/healthz")
    def healthz():
        # liveness only; this stays ok while the monitored service is down
        info = app.config["SLO_WATCH"]
        return jsonify({"status": "ok", "uptime_seconds": round(time.time() - info["started_at"], 1),
                        "services": len(config.services)})

    return app


def _service(config: Config, name: str):
    """Looks up a service or aborts with 404.

    Raising LookupError here meant a typo in the URL came back as a 500, which reads as
    "the monitor is broken" when in fact the monitor is fine and the URL is not.
    """
    for service in config.services:
        if service.name == name:
            return service
    abort(404, description=f"unknown service {name!r}")


def _prometheus(config: Config, monitor: Monitor, now: float) -> str:
    lines: list[str] = []
    lines.append("# HELP slo_compliance_ratio Good events divided by valid events.")
    lines.append("# TYPE slo_compliance_ratio gauge")
    rows = monitor.slo_table(now)
    for row in rows:
        labels = f'service="{row["service"]}",slo="{row["slo"]}",sli="{row["sli"]}"'
        if row["compliance"] is not None:
            lines.append(f"slo_compliance_ratio{{{labels}}} {row['compliance'] / 100:.6f}")
        if row["burn_rate"] is not None:
            lines.append(f'slo_burn_rate{{{labels}}} {row["burn_rate"]:.6f}')
        if row["budget_remaining"] is not None:
            lines.append(f'slo_budget_remaining_ratio{{{labels}}} {row["budget_remaining"]:.6f}')
        lines.append(f'slo_bad_events_total{{{labels}}} {row["total"] - row["good"]}')
        lines.append(f'slo_valid_events_total{{{labels}}} {row["total"]}')
    lines.append("# HELP slo_alerts_firing Alerts currently firing.")
    lines.append("# TYPE slo_alerts_firing gauge")
    lines.append(f"slo_alerts_firing {len(monitor.store.active_alerts())}")
    return "\n".join(lines) + "\n"
