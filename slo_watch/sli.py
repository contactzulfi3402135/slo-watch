"""Burn rate and error budget.

burn = (bad / valid) / (1 - objective), so 1.0 spends the budget exactly when the window
closes. Every threshold in the config is a multiple of that number. Kept in one file with
tests that pin the arithmetic, because getting it wrong produces alerts that fire on a
healthy service or stay quiet through a real outage.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import SLO
from .store import Counts


@dataclass(frozen=True)
class SLIReport:
    slo: str
    sli: str
    objective: float
    good: int
    total: int
    burn_rate: float | None
    budget_remaining: float | None      # fraction, 1.0 = untouched, 0.0 = exhausted
    sufficient_data: bool

    @property
    def ratio(self) -> float | None:
        return self.good / self.total if self.total else None

    def as_dict(self) -> dict:
        return {
            "slo": self.slo,
            "sli": self.sli,
            "objective": self.objective,
            "good": self.good,
            "total": self.total,
            "compliance": None if self.ratio is None else round(self.ratio * 100, 4),
            "burn_rate": None if self.burn_rate is None else round(self.burn_rate, 3),
            "budget_remaining": None if self.budget_remaining is None else round(self.budget_remaining, 4),
            "sufficient_data": self.sufficient_data,
        }


def burn_rate(counts: Counts, objective_pct: float, min_events: int = 1) -> float | None:
    """Bad-event ratio divided by the budget ratio.

    Returns None when there is not enough data to say anything. None is not zero: a
    monitor that reports burn 0.0 for a service it has never probed is lying.
    """
    if counts.total < min_events or counts.total == 0:
        return None
    allowed_bad = 1.0 - (objective_pct / 100.0)
    if allowed_bad <= 0:
        return None
    observed_bad = counts.bad / counts.total
    return observed_bad / allowed_bad


def budget_remaining(counts: Counts, objective_pct: float) -> float | None:
    """Fraction of the error budget still unspent over the window."""
    if counts.total == 0:
        return None
    allowed_bad = 1.0 - (objective_pct / 100.0)
    if allowed_bad <= 0:
        return 0.0
    allowed_count = allowed_bad * counts.total
    spent = counts.bad
    remaining = (allowed_count - spent) / allowed_count if allowed_count else 0.0
    return max(0.0, min(1.0, remaining))


def evaluate_sli(slo: SLO, counts: Counts) -> SLIReport:
    return SLIReport(
        slo=slo.name,
        sli=slo.sli,
        objective=slo.objective,
        good=counts.good,
        total=counts.total,
        burn_rate=burn_rate(counts, slo.objective, slo.min_events),
        budget_remaining=budget_remaining(counts, slo.objective),
        sufficient_data=counts.total >= slo.min_events,
    )


def time_to_exhaustion_hours(counts: Counts, objective_pct: float, window_seconds: int,
                             min_events: int = 1) -> float | None:
    """How long until the budget is gone if the current burn rate holds.

    None when there is nothing to extrapolate from: no data, or a burn rate of zero,
    which would otherwise divide the answer into infinity and look like good news
    rendered as a number.
    """
    rate = burn_rate(counts, objective_pct, min_events)
    remaining = budget_remaining(counts, objective_pct)
    if rate is None or remaining is None or rate <= 0:
        return None
    return (remaining * (window_seconds / 3600.0)) / rate
