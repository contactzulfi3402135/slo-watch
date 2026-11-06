"""argparse entry point: init, probe, report, eval, serve."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .alerts import Evaluator, build_notifiers
from .config import ConfigError, load_config
from .monitor import Monitor
from .store import Store
from .windows import format_window

log = logging.getLogger("slo_watch.cli")

EXAMPLE_CONFIG = """\
# slo-watch configuration. Everything here is validated at startup.
database: slo-watch.db
interval_seconds: 5           # how often to probe
evaluate_seconds: 10          # how often to evaluate the alert rules
retention: 30d                # how much raw probe history to keep

services:
  - name: checkout-api
    probes:
      - type: http
        url: http://localhost:8099/health
        method: GET
        timeout_ms: 2000
        expect_status: 200
      - type: command
        command: test -f /tmp/checkout-ready
        timeout_ms: 1000
    slos:
      - name: availability
        sli: availability
        objective: 99.5
        window: 28d
      - name: latency
        sli: latency
        objective: 95.0
        window: 28d
        latency_threshold_ms: 250

# Burn rate is measured against the objective: 1x spends the budget exactly at the end of
# the window, 14.4x spends it in a day (99.5% objective, 28 day window).
rules:
  - name: fast-burn
    long_window: 5m
    short_window: 1m
    burn_rate: 5
    severity: page
    renotify: 5m
    min_events: 6               # must fit inside short_window at this probe interval
  - name: slow-burn
    long_window: 1h
    short_window: 10m
    burn_rate: 2
    severity: ticket
    renotify: 1h
    min_events: 20

notifiers:
  - type: log
"""


def _load(args) -> tuple:
    config = load_config(args.config)
    # containers mount the volume somewhere the image can't know about
    override = os.environ.get("SLO_WATCH_DB")
    if override:
        config = replace(config, database=Path(override))
    store = Store(config.database)
    monitor = Monitor(config, store, Evaluator(store, build_notifiers(config.notifiers)))
    return config, store, monitor


def cmd_init(args) -> int:
    path = Path(args.config)
    if path.exists() and not args.force:
        print(f"{path} already exists; pass --force to overwrite", file=sys.stderr)
        return 1
    path.write_text(EXAMPLE_CONFIG)
    print(f"wrote {path}")
    print("next: slo-watch probe --config " + str(path))
    return 0


def cmd_probe(args) -> int:
    config, store, monitor = _load(args)
    results = monitor.probe_all()
    for result in results:
        mark = "ok  " if result.ok else "FAIL"
        print(f"{mark} {result.service:<20} {result.latency_ms:7.1f}ms  {result.detail}")
    store.close()
    # non-zero exit so cron can page on it
    return 0 if all(r.ok for r in results) else 1


def cmd_report(args) -> int:
    config, store, monitor = _load(args)
    rows = monitor.slo_table()
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        header = f"{'service':<20} {'slo':<13} {'objective':>9} {'compliance':>11} {'burn':>7} {'budget':>7} {'exhausted in':>13}"
        print(header)
        print("-" * len(header))
        for row in rows:
            compliance = "n/a" if row["compliance"] is None else f"{row['compliance']:.3f}%"
            burn = "n/a" if row["burn_rate"] is None else f"{row['burn_rate']:.2f}x"
            budget = "n/a" if row["budget_remaining"] is None else f"{row['budget_remaining'] * 100:.1f}%"
            exhausted = ("n/a" if row["exhausted_in_hours"] is None
                         else f"{row['exhausted_in_hours']:.1f}h")
            print(f"{row['service']:<20} {row['slo']:<13} {row['objective']:>8.2f}% "
                  f"{compliance:>11} {burn:>7} {budget:>7} {exhausted:>13}")
        print()
        if rows:
            window = format_window(config.services[0].slos[0].window_seconds)
            retention = format_window(config.retention_seconds)
            total = sum(row["total"] for row in rows)
            note = "  (retention is shorter, so the numbers above cover the retained rows)" \
                if config.retention_seconds < config.services[0].slos[0].window_seconds else ""
            print(f"{total} valid events · slo window {window} · raw retention {retention}{note}")
        else:
            print("no slos configured")
    store.close()
    return 0


def cmd_eval(args) -> int:
    config, store, monitor = _load(args)
    decisions = monitor.evaluate()
    for d in decisions:
        print(f"{d.state:<9} {d.service}/{d.slo}/{d.rule}  burn(long)={_fmt(d.burn_long)} "
              f"burn(short)={_fmt(d.burn_short)}  notify={d.notify}  ({d.reason})")
    if not decisions:
        print("nothing to do: every alert is in the state the data says it should be in")
    store.close()
    return 0


def cmd_serve(args) -> int:
    from .api import create_app
    from .scheduler import Scheduler

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    config, store, monitor = _load(args)
    scheduler = Scheduler(monitor)
    scheduler.start()
    app = create_app(config, monitor)
    log.info("serving on http://%s:%d", args.host, args.port)
    try:
        # threaded: the manual probe endpoint blocks on an outbound request
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    finally:
        scheduler.stop()
        store.close()
    return 0


def _fmt(value) -> str:
    return "n/a" if value is None else f"{value:.2f}x"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slo-watch",
        description="A small SLO monitor: probes a service, records the results in SQLite "
                    "and alerts on multi-window burn rate.",
    )
    parser.add_argument("--version", action="version", version=f"slo-watch {__version__}")
    parser.add_argument("-c", "--config", default="config.yml", help="path to config.yml")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a starter config.yml")
    init.add_argument("--force", action="store_true", help="overwrite an existing config")
    init.set_defaults(func=cmd_init)

    probe = sub.add_parser("probe", help="run every probe once and exit")
    probe.set_defaults(func=cmd_probe)

    report = sub.add_parser("report", help="print the SLI table")
    report.add_argument("--json", action="store_true", help="machine-readable output")
    report.set_defaults(func=cmd_report)

    evaluate = sub.add_parser("eval", help="evaluate the alert rules once")
    evaluate.set_defaults(func=cmd_eval)

    serve = sub.add_parser("serve", help="run the probes, the evaluator and the HTTP API")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
