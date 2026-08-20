"""DailyGovernor: budget accounting and OK/WARN/SKIP verdicts (§5.4)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from copier.config import GovernorConfig, RiskConfig
from copier.core.governor import DailyGovernor
from copier.models import GovernorVerdict

RISK = RiskConfig(mae_points=4.64, daily_dd_limit=2500)
GOV = GovernorConfig(soft_threshold_pct=0.60, hard_threshold_pct=0.85)
DAY = date(2025, 8, 18)


def _gov() -> DailyGovernor:
    return DailyGovernor(RISK, GOV)


def test_ok_below_soft_threshold():
    a = _gov().assess(Decimal("371.2"), DAY)  # 14.8% of 2500
    assert a.verdict is GovernorVerdict.OK
    assert a.budget_remaining_usd == Decimal("2128.8")
    assert a.utilisation_pct == pytest.approx(0.14848)


def test_warn_between_soft_and_hard():
    a = _gov().assess(Decimal("1700"), DAY)  # 68% → WARN
    assert a.verdict is GovernorVerdict.WARN


def test_skip_above_hard_threshold():
    a = _gov().assess(Decimal("2200"), DAY)  # 88% → SKIP
    assert a.verdict is GovernorVerdict.SKIP


def test_assess_does_not_commit():
    g = _gov()
    g.assess(Decimal("1000"), DAY)
    assert g.committed_on(DAY) == Decimal("0")


def test_commit_accumulates_within_a_day():
    g = _gov()
    g.commit(Decimal("1000"), DAY)
    a = g.commit(Decimal("1000"), DAY)
    assert g.committed_on(DAY) == Decimal("2000")
    assert a.verdict is GovernorVerdict.WARN  # 2000/2500 = 80%


def test_days_are_independent():
    g = _gov()
    g.commit(Decimal("2000"), DAY)
    other = date(2025, 8, 19)
    a = g.assess(Decimal("100"), other)
    assert a.committed_usd == Decimal("100")
    assert a.verdict is GovernorVerdict.OK


def test_budget_can_go_negative_but_still_advises():
    g = _gov()
    a = g.commit(Decimal("3000"), DAY)  # over the 2500 limit
    assert a.verdict is GovernorVerdict.SKIP
    assert a.budget_remaining_usd == Decimal("-500")
