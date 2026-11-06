import pytest

from slo_watch.windows import WindowError, format_window, parse_window


@pytest.mark.parametrize("text,seconds", [
    ("30s", 30), ("5m", 300), ("1h", 3600), ("6h", 21600), ("28d", 2419200), ("2w", 1209600),
])
def test_parse_window(text, seconds):
    assert parse_window(text) == seconds


@pytest.mark.parametrize("text", ["", "5", "m", "5x", "-5m", "0m", "1.5h"])
def test_parse_window_rejects_nonsense(text):
    with pytest.raises(WindowError):
        parse_window(text)


def test_format_round_trips():
    assert format_window(parse_window("6h")) == "6h"
    assert format_window(parse_window("28d")) == "28d"
