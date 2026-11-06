"""Three loops on three threads: probe keeps its cadence when evaluation is slow, and
pruning can't hold up a probe. Each one wraps its body in try/except so a bad iteration
logs and the loop continues."""

from __future__ import annotations

import logging
import threading

from .monitor import Monitor

log = logging.getLogger("slo_watch.scheduler")


class Scheduler:
    def __init__(self, monitor: Monitor):
        self.monitor = monitor
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        cfg = self.monitor.config
        self._spawn("probe", cfg.interval_seconds, self._probe_once)
        self._spawn("evaluate", cfg.evaluate_seconds, self._evaluate_once)
        self._spawn("prune", 3600, self._prune_once)
        log.info("scheduler started: probe every %ss, evaluate every %ss",
                 cfg.interval_seconds, cfg.evaluate_seconds)

    def _spawn(self, name: str, interval: float, fn) -> None:
        def loop():
            # run once at start, or the monitor looks empty for a whole interval
            fn()
            while not self._stop.wait(interval):
                fn()

        thread = threading.Thread(target=loop, name=f"slo-watch-{name}", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _probe_once(self) -> None:
        try:
            results = self.monitor.probe_all()
            failed = [r for r in results if not r.ok]
            if failed:
                log.warning("%d/%d probes failed: %s", len(failed), len(results),
                            ", ".join(f"{r.service}({r.detail or 'failed'})" for r in failed[:5]))
        except Exception:
            log.exception("probe loop iteration failed; continuing")

    def _evaluate_once(self) -> None:
        try:
            self.monitor.evaluate()
        except Exception:
            log.exception("evaluate loop iteration failed; continuing")

    def _prune_once(self) -> None:
        try:
            removed = self.monitor.prune()
            if removed:
                log.info("pruned %d old probe rows", removed)
        except Exception:
            log.exception("prune loop iteration failed; continuing")

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads.clear()
