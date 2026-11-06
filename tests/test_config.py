import pytest

from slo_watch.config import ConfigError, load_config, parse_config

MINIMAL = {
    "services": [{
        "name": "api",
        "probes": [{"type": "http", "url": "http://127.0.0.1:9/health"}],
        "slos": [{"name": "availability", "sli": "availability", "objective": 99.5, "window": "28d"}],
    }],
    "rules": [{"name": "fast", "long_window": "1h", "short_window": "5m", "burn_rate": 14.4}],
}


def test_minimal_config_parses():
    cfg = parse_config(MINIMAL)
    assert cfg.services[0].name == "api"
    assert cfg.services[0].slos[0].window_seconds == 2419200
    assert cfg.rules[0].severity == "page"
    assert cfg.notifiers[0].type == "log"


def test_objective_must_be_a_percentage():
    bad = {**MINIMAL, "services": [{**MINIMAL["services"][0],
           "slos": [{"name": "a", "sli": "availability", "objective": 150}]}]}
    with pytest.raises(ConfigError, match="objective"):
        parse_config(bad)


def test_sli_must_be_known():
    bad = {**MINIMAL, "services": [{**MINIMAL["services"][0],
           "slos": [{"name": "a", "sli": "vibes", "objective": 99}]}]}
    with pytest.raises(ConfigError, match="sli"):
        parse_config(bad)


def test_short_window_cannot_exceed_long_window():
    bad = {**MINIMAL, "rules": [{"name": "r", "long_window": "5m", "short_window": "1h", "burn_rate": 5}]}
    with pytest.raises(ConfigError, match="short_window"):
        parse_config(bad)


def test_duplicate_service_names_rejected():
    bad = {**MINIMAL, "services": [MINIMAL["services"][0], MINIMAL["services"][0]]}
    with pytest.raises(ConfigError, match="duplicate"):
        parse_config(bad)


def test_missing_probe_url_is_an_error_not_a_runtime_surprise():
    bad = {**MINIMAL, "services": [{"name": "api", "probes": [{"type": "http"}],
                                    "slos": MINIMAL["services"][0]["slos"]}]}
    with pytest.raises(ConfigError, match="needs a url"):
        parse_config(bad)


def test_load_config_reports_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yml")


def test_example_config_in_repo_is_valid(tmp_path):
    from slo_watch.cli import EXAMPLE_CONFIG
    path = tmp_path / "config.yml"
    path.write_text(EXAMPLE_CONFIG)
    cfg = load_config(path)
    assert len(cfg.rules) == 2
    assert cfg.database.name == "slo-watch.db"


def test_min_events_must_fit_in_the_short_window():
    bad = {**MINIMAL, "interval_seconds": 15,
           "rules": [{"name": "fast", "long_window": "5m", "short_window": "1m",
                      "burn_rate": 5, "min_events": 60}]}
    with pytest.raises(ConfigError, match="cannot be met"):
        parse_config(bad)

    ok = {**MINIMAL, "interval_seconds": 15,
          "rules": [{"name": "fast", "long_window": "5m", "short_window": "1m",
                     "burn_rate": 5, "min_events": 4}]}
    assert parse_config(ok).rules[0].min_events == 4
