"""YAML to typed objects.

Everything is checked at startup and the error names the key that's wrong. The alternative
is finding out at 3am from a thread that quietly died.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .windows import WindowError, parse_window

VALID_SLIS = ("availability", "latency")
VALID_SEVERITIES = ("page", "ticket", "info")


class ConfigError(ValueError):
    """Raised when the config file is not usable."""


@dataclass(frozen=True)
class Probe:
    type: str                     # "http" | "command"
    url: str = ""
    command: str = ""
    method: str = "GET"
    timeout_ms: int = 2000
    expect_status: int = 200
    # status code only, so a 200 login page still counts as up. Assertions go in a
    # command probe.
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SLO:
    name: str
    objective: float              # percentage, e.g. 99.5
    window_seconds: int           # evaluation window, e.g. 28 days
    sli: str                      # "availability" | "latency"
    latency_threshold_ms: int = 250
    min_events: int = 20          # below this, burn rate is not reported

    @property
    def error_budget_ratio(self) -> float:
        """Fraction of events allowed to be bad, e.g. 0.005 for a 99.5% objective."""
        return 1.0 - (self.objective / 100.0)


@dataclass(frozen=True)
class Service:
    name: str
    probes: tuple[Probe, ...]
    slos: tuple[SLO, ...]


@dataclass(frozen=True)
class AlertRule:
    name: str
    long_window: int
    short_window: int
    burn_rate: float
    severity: str
    # without this a long incident either spams or goes quiet
    renotify_seconds: int = 3600
    min_events: int = 20


@dataclass(frozen=True)
class NotifierConfig:
    type: str                     # "log" | "webhook"
    url: str = ""
    timeout_ms: int = 2000


@dataclass(frozen=True)
class Config:
    database: Path
    interval_seconds: int
    retention_seconds: int
    evaluate_seconds: int
    services: tuple[Service, ...]
    rules: tuple[AlertRule, ...]
    notifiers: tuple[NotifierConfig, ...]
    source: Path | None = None


def _require(mapping, key, where, kind=str):
    if key not in mapping:
        raise ConfigError(f"{where}: missing required key {key!r}")
    value = mapping[key]
    if not isinstance(value, kind):
        raise ConfigError(f"{where}.{key}: expected {kind.__name__}, got {type(value).__name__}")
    return value


def _window(value, where) -> int:
    try:
        return parse_window(value)
    except WindowError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def parse_config(raw: dict, source: Path | None = None) -> Config:
    """Validate a parsed YAML mapping into typed config. Never mutates `raw`."""
    if not isinstance(raw, dict):
        raise ConfigError("top level of the config must be a mapping")

    interval = int(raw.get("interval_seconds", 15))
    if interval < 1:
        raise ConfigError("interval_seconds must be at least 1")
    evaluate = int(raw.get("evaluate_seconds", 30))
    if evaluate < 1:
        raise ConfigError("evaluate_seconds must be at least 1")

    services: list[Service] = []
    seen_services: set[str] = set()
    for i, svc in enumerate(raw.get("services") or []):
        where = f"services[{i}]"
        name = _require(svc, "name", where)
        if name in seen_services:
            raise ConfigError(f"{where}: duplicate service name {name!r}")
        seen_services.add(name)

        probes: list[Probe] = []
        for j, p in enumerate(svc.get("probes") or []):
            pw = f"{where}.probes[{j}]"
            ptype = _require(p, "type", pw)
            if ptype not in ("http", "command"):
                raise ConfigError(f"{pw}.type: must be 'http' or 'command', got {ptype!r}")
            if ptype == "http" and not p.get("url"):
                raise ConfigError(f"{pw}: http probe needs a url")
            if ptype == "command" and not p.get("command"):
                raise ConfigError(f"{pw}: command probe needs a command")
            probes.append(Probe(
                type=ptype,
                url=p.get("url", ""),
                command=p.get("command", ""),
                method=str(p.get("method", "GET")).upper(),
                timeout_ms=int(p.get("timeout_ms", 2000)),
                expect_status=int(p.get("expect_status", 200)),
                headers=dict(p.get("headers") or {}),
            ))
        if not probes:
            raise ConfigError(f"{where}: a service needs at least one probe")

        slos: list[SLO] = []
        seen_slos: set[str] = set()
        for j, s in enumerate(svc.get("slos") or []):
            sw = f"{where}.slos[{j}]"
            sname = _require(s, "name", sw)
            if sname in seen_slos:
                raise ConfigError(f"{sw}: duplicate slo name {sname!r}")
            seen_slos.add(sname)
            sli = _require(s, "sli", sw)
            if sli not in VALID_SLIS:
                raise ConfigError(f"{sw}.sli: must be one of {VALID_SLIS}, got {sli!r}")
            objective = float(_require(s, "objective", sw, (int, float)))
            if not 0 < objective < 100:
                raise ConfigError(f"{sw}.objective: must be between 0 and 100 exclusive")
            slos.append(SLO(
                name=sname,
                objective=objective,
                window_seconds=_window(s.get("window", "28d"), f"{sw}.window"),
                sli=sli,
                latency_threshold_ms=int(s.get("latency_threshold_ms", 250)),
                min_events=int(s.get("min_events", 20)),
            ))
        if not slos:
            raise ConfigError(f"{where}: a service needs at least one slo")
        services.append(Service(name=name, probes=tuple(probes), slos=tuple(slos)))

    if not services:
        raise ConfigError("config has no services")

    rules: list[AlertRule] = []
    for i, r in enumerate(raw.get("rules") or []):
        where = f"rules[{i}]"
        rule = AlertRule(
            name=_require(r, "name", where),
            long_window=_window(_require(r, "long_window", where), f"{where}.long_window"),
            short_window=_window(_require(r, "short_window", where), f"{where}.short_window"),
            burn_rate=float(_require(r, "burn_rate", where, (int, float))),
            severity=str(r.get("severity", "page")),
            renotify_seconds=_window(str(r.get("renotify", "1h")), f"{where}.renotify"),
            min_events=int(r.get("min_events", 20)),
        )
        if rule.severity not in VALID_SEVERITIES:
            raise ConfigError(f"{where}.severity: must be one of {VALID_SEVERITIES}")
        if rule.burn_rate <= 0:
            raise ConfigError(f"{where}.burn_rate: must be positive")
        if rule.short_window > rule.long_window:
            raise ConfigError(f"{where}: short_window must be <= long_window")
        # A short window only ever holds short_window / interval samples, so a rule asking
        # for more than that can never fire. Silently dead. Check it here instead of
        # finding out during the incident.
        capacity = rule.short_window // interval
        if rule.min_events > capacity:
            raise ConfigError(
                f"{where}.min_events: {rule.min_events} cannot be met by a "
                f"{rule.short_window}s short window at interval_seconds={interval} "
                f"(at most {capacity} probes); lower min_events or widen short_window"
            )
        rules.append(rule)
    if not rules:
        raise ConfigError("config has no alert rules")

    notifiers: list[NotifierConfig] = []
    for i, n in enumerate(raw.get("notifiers") or [{"type": "log"}]):
        where = f"notifiers[{i}]"
        ntype = _require(n, "type", where)
        if ntype not in ("log", "webhook"):
            raise ConfigError(f"{where}.type: must be 'log' or 'webhook'")
        if ntype == "webhook" and not n.get("url"):
            raise ConfigError(f"{where}: webhook notifier needs a url")
        notifiers.append(NotifierConfig(type=ntype, url=n.get("url", ""),
                                        timeout_ms=int(n.get("timeout_ms", 2000))))

    return Config(
        database=Path(raw.get("database", "slo-watch.db")),
        interval_seconds=interval,
        retention_seconds=_window(str(raw.get("retention", "30d")), "retention"),
        evaluate_seconds=evaluate,
        services=tuple(services),
        rules=tuple(rules),
        notifiers=tuple(notifiers),
        source=source,
    )


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: invalid yaml: {exc}") from exc
    return parse_config(raw or {}, source=p)
