import pytest

from slo_watch.config import SLO
from slo_watch.sli import burn_rate, budget_remaining, evaluate_sli, time_to_exhaustion_hours
from slo_watch.store import Counts

SLO_995 = SLO(name="availability", objective=99.5, window_seconds=28 * 86400, sli="availability")


def test_no_data_reports_none_not_zero():
    assert burn_rate(Counts(good=0, total=0), 99.5) is None
    assert budget_remaining(Counts(good=0, total=0), 99.5) is None


def test_exact_objective_is_burn_rate_one():
    # 99.5% objective over 1000 events allows 5 bad ones. Exactly 5 bad = spending at
    # exactly the rate that exhausts the budget at the end of the window.
    assert burn_rate(Counts(good=995, total=1000), 99.5) == pytest.approx(1.0)


def test_fast_burn_matches_the_sre_workbook_table():
    # 14.4x burn over a 28 day window spends the budget in ~2 days (1/14.4 of 28d).
    counts = Counts(good=9928, total=10000)      # 72 bad = 0.72% vs 0.5% allowed
    assert burn_rate(counts, 99.5) == pytest.approx(1.44, rel=1e-6)


def test_perfect_service_has_zero_burn():
    assert burn_rate(Counts(good=100, total=100), 99.9) == pytest.approx(0.0)


def test_min_events_gate_suppresses_thin_windows():
    # Three samples, all bad, is a 200x burn rate and means nothing.
    assert burn_rate(Counts(good=0, total=3), 99.9, min_events=20) is None


def test_budget_remaining_is_clamped():
    assert budget_remaining(Counts(good=1000, total=1000), 99.5) == pytest.approx(1.0)
    assert budget_remaining(Counts(good=0, total=1000), 99.5) == pytest.approx(0.0)


def test_budget_remaining_halves_at_one_burn_rate():
    counts = Counts(good=995, total=1000)
    assert budget_remaining(counts, 99.5) == pytest.approx(0.0)   # budget exactly spent
    counts = Counts(good=9975, total=10000)                        # 2.5 bad vs 5 allowed
    assert budget_remaining(counts, 99.5) == pytest.approx(0.5)


def test_time_to_exhaustion_uses_the_window():
    counts = Counts(good=995, total=1000)                          # burn 1.0, nothing left
    assert time_to_exhaustion_hours(counts, 99.5, 28 * 86400) == pytest.approx(0.0)
    counts = Counts(good=9975, total=10000)                        # burn 0.5, half budget left
    hours = time_to_exhaustion_hours(counts, 99.5, 28 * 86400)
    assert hours == pytest.approx((28 * 24 / 0.5) * 0.5)


def test_evaluate_sli_reports_compliance_and_sufficiency():
    report = evaluate_sli(SLO_995, Counts(good=990, total=1000))
    assert report.ratio == pytest.approx(0.99)
    assert report.sufficient_data is True
    thin = evaluate_sli(SLO_995, Counts(good=4, total=4))
    assert thin.sufficient_data is False
    assert thin.burn_rate is None
