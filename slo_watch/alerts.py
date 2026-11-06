"""Multi-window burn-rate alerting.

Long window and short window both have to be over the threshold before an alert fires, and
either one dropping under it resolves. That's the multiwindow, multi-burn-rate pattern from
the SRE workbook. Watching only the long window leaves alerts firing an hour after the
incident; watching only the short one flaps on every blip.

Notifying is separate from state, so an alert that stays broken doesn't re-page on every
tick and a resolved one sends exactly one resolve.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .config import AlertRule, NotifierConfig, SLO, Service
from .sli import burn_rate, evaluate_sli
from .store import AlertRow, Counts, Store

log = logging.getLogger("slo_watch.alerts")


@dataclass(frozen=True)
class Decision:
    fingerprint: str
    service: str
    slo: str
    rule: str
    severity: str
    state: str                  # "firing" | "resolved"
    burn_long: float | None
    burn_short: float | None
    notify: bool
    reason: str


class Notifier(Protocol):
    def send(self, decision: Decision, alert: AlertRow) -> None: ...


class LogNotifier:
    def send(self, decision: Decision, alert: AlertRow) -> None:
        log.warning(
            "ALERT %s %s/%s %s burn(long)=%s burn(short)=%s notifications=%d",
            decision.state.upper(), decision.service, decision.slo, decision.rule,
            _fmt(decision.burn_long), _fmt(decision.burn_short), alert.notifications,
        )


class WebhookNotifier:
    """POSTs a small JSON payload. One retry, short timeout, failures only logged.

    A broken webhook must never stop the monitor from monitoring, so nothing here
    raises: the alert is already persisted by the time this runs.
    """

    def __init__(self, url: str, timeout_ms: int = 2000):
        self.url = url
        self.timeout = timeout_ms / 1000

    def send(self, decision: Decision, alert: AlertRow) -> None:
        payload = json.dumps({
            "fingerprint": decision.fingerprint,
            "service": decision.service,
            "slo": decision.slo,
            "rule": decision.rule,
            "severity": decision.severity,
            "state": decision.state,
            "burn_rate_long": decision.burn_long,
            "burn_rate_short": decision.burn_short,
            "started_at": alert.started_at,
            "resolved_at": alert.resolved_at,
            "notifications": alert.notifications,
        }).encode()
        for attempt in (1, 2):
            request = urllib.request.Request(
                self.url, data=payload, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "slo-watch/1.0"},
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout):
                    return
            except (urllib.error.URLError, OSError) as exc:
                if attempt == 2:
                    log.error("webhook delivery failed after 2 attempts: %s", exc)


class RecordingNotifier:
    """Keeps everything it is given. Used by the tests and by `slo-watch eval`."""

    def __init__(self) -> None:
        self.sent: list[tuple[Decision, AlertRow]] = []

    def send(self, decision: Decision, alert: AlertRow) -> None:
        self.sent.append((decision, alert))


def build_notifiers(configs: tuple[NotifierConfig, ...]) -> list[Notifier]:
    out: list[Notifier] = []
    for cfg in configs:
        if cfg.type == "log":
            out.append(LogNotifier())
        elif cfg.type == "webhook":
            out.append(WebhookNotifier(cfg.url, cfg.timeout_ms))
    return out


def fingerprint(service: str, slo: str, rule: str) -> str:
    return f"{service}/{slo}/{rule}"


class Evaluator:
    """The alert state machine plus its notifiers."""

    def __init__(self, store: Store, notifiers: list[Notifier] | None = None):
        self.store = store
        self.notifiers: list[Notifier] = notifiers if notifiers is not None else [LogNotifier()]

    def tick(self, services: tuple[Service, ...], rules: tuple[AlertRule, ...],
             now: float) -> list[Decision]:
        decisions: list[Decision] = []
        for service in services:
            for slo in service.slos:
                for rule in rules:
                    decision = self._evaluate_one(service, slo, rule, now)
                    if decision is not None:
                        decisions.append(decision)
        return decisions

    def _evaluate_one(self, service: Service, slo: SLO, rule: AlertRule,
                      now: float) -> Decision | None:
        min_events = max(rule.min_events, slo.min_events)
        burn_long = burn_rate(self._counts(service.name, slo, now - rule.long_window),
                              slo.objective, min_events)
        burn_short = burn_rate(self._counts(service.name, slo, now - rule.short_window),
                               slo.objective, min_events)

        fp = fingerprint(service.name, slo.name, rule.name)
        existing = self.store.get_alert(fp)
        was_firing = existing is not None and existing.state == "firing"

        if burn_long is None:
            # no data in the long window: hold whatever state we're in, don't resolve
            return None

        if burn_short is None:
            # long window says trouble, short window has nothing to say about it:
            # keep an existing alert up, but don't open a new one on one window alone
            if not was_firing:
                return None
            self.store.upsert_alert(
                fingerprint=fp, service=service.name, slo=slo.name, rule=rule.name,
                severity=rule.severity, state="firing", now=now,
                burn_long=burn_long, burn_short=None, notified=False,
            )
            return Decision(fp, service.name, slo.name, rule.name, rule.severity, "firing",
                            burn_long, None, False, "short window has no data; holding state")

        should_fire = burn_long >= rule.burn_rate and burn_short >= rule.burn_rate

        if should_fire:
            notify = not was_firing or _due(existing, rule, now)
            state = "firing"
            reason = "still burning" if was_firing else "both windows above the burn threshold"
        elif was_firing:
            notify = True
            state = "resolved"
            reason = "burn back below the long-window threshold"
        else:
            return None

        alert = self.store.upsert_alert(
            fingerprint=fp, service=service.name, slo=slo.name, rule=rule.name,
            severity=rule.severity, state=state, now=now,
            burn_long=burn_long, burn_short=burn_short, notified=notify,
        )
        decision = Decision(fp, service.name, slo.name, rule.name, rule.severity, state,
                            burn_long, burn_short, notify, reason)

        if notify:
            for notifier in self.notifiers:
                try:
                    notifier.send(decision, alert)
                except Exception:                     # a notifier bug must not stop the monitor
                    log.exception("notifier %r raised", notifier)
        return decision

    def _counts(self, service: str, slo: SLO, since_ts: float) -> Counts:
        if slo.sli == "latency":
            return self.store.latency_counts(service, since_ts, slo.latency_threshold_ms)
        return self.store.availability_counts(service, since_ts)


def _due(existing: AlertRow | None, rule: AlertRule, now: float) -> bool:
    if existing is None or existing.last_notified is None:
        return True
    return (now - existing.last_notified) >= rule.renotify_seconds


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}x"


def slo_report(store: Store, service: Service, slo: SLO, now: float):
    if slo.sli == "latency":
        counts = store.latency_counts(service.name, now - slo.window_seconds,
                                      slo.latency_threshold_ms)
    else:
        counts = store.availability_counts(service.name, now - slo.window_seconds)
    return evaluate_sli(slo, counts)
