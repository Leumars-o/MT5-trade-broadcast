"""Formatter golden-file tests (§8). Messages are pure functions of their
inputs (``now`` passed in), so their exact output is pinned to files under
``tests/golden/``. Regenerate with ``UPDATE_GOLDEN=1 pytest tests/test_formatter.py``.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from copier.config import GovernorConfig
from copier.core.governor import DailyGovernor
from copier.core.risk import estimate_close, size_position
from copier.models import SizingDecision, SizingRefused, SymbolSpec
from copier.notify.formatter import (
    fmt_age,
    fmt_held,
    format_close,
    format_entry,
    format_pre_existing,
    format_refusal,
)

from .conftest import at, make_position
from .test_risk import CFG, XAU

GOLDEN = Path(__file__).parent / "golden"
GOV = GovernorConfig(soft_threshold_pct=0.60, hard_threshold_pct=0.85)
DAY = date(2025, 8, 18)


def _check(name: str, actual: str) -> None:
    path = GOLDEN / f"{name}.txt"
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual + "\n")
    expected = path.read_text().rstrip("\n")
    assert actual == expected, f"\n--- expected ---\n{expected}\n--- actual ---\n{actual}"


# ---------------------------------------------------------------- unit helpers


def test_fmt_age():
    ref = at(0)
    assert fmt_age(ref, ref) == "0.0s"
    assert fmt_age(ref + timedelta(milliseconds=1400), ref) == "1.4s"
    assert fmt_age(ref + timedelta(seconds=185), ref) == "3m 05s"
    # A snapshot slightly behind "now" clamps to zero, never negative.
    assert fmt_age(ref - timedelta(seconds=5), ref) == "0.0s"


def test_fmt_held():
    assert fmt_held(at(0), at(134)) == "2h 14m"
    assert fmt_held(at(0), at(14)) == "14m"


# ---------------------------------------------------------------- golden messages


def test_entry_ok():
    pos = make_position("96484066", direction="sell", volume=0.001798,
                        open_price=4399.36, open_time=at(0))
    sizing = size_position(pos, XAU, CFG)
    assert isinstance(sizing, SizingDecision)
    assessment = DailyGovernor(CFG, GOV).commit(sizing.risk_usd, DAY)
    now = at(0).replace(second=1, microsecond=400000)
    _check("entry_ok", format_entry(pos, sizing, assessment, "XAUUSD", now))


def test_entry_warn_verdict():
    # A leveraged multiplier hits the risk cap (0.64 lots) — the entry shows the
    # native multiple + risk-capped note, and the governor warns.
    cfg = CFG.model_copy(update={"size_multiplier": 20.0})
    pos = make_position("96484099", direction="buy", volume=0.02,
                        open_price=4390.00, open_time=at(0))
    sizing = size_position(pos, XAU, cfg)
    assert isinstance(sizing, SizingDecision)
    gov = DailyGovernor(cfg, GOV)
    gov.commit(Decimal("1700"), DAY)  # pre-load the day near the soft threshold
    assessment = gov.commit(sizing.risk_usd, DAY)
    now = at(0).replace(second=0, microsecond=900000)
    _check("entry_warn", format_entry(pos, sizing, assessment, "XAUUSD", now))


def test_pre_existing():
    pos = make_position("96484070", direction="buy", volume=0.01,
                        open_price=4390.00, open_time=at(-5))
    now = at(0).replace(microsecond=900000)
    _check("pre_existing", format_pre_existing(pos, "XAUUSD", now))


def test_close():
    pos = make_position("96484066", direction="sell", volume=0.40,
                        open_price=4399.36, open_time=at(0))
    pos = pos.with_updates(close_price=4392.34, close_time=at(134))
    est = estimate_close(pos, XAU, destination_lots=0.64, cfg=CFG)
    now = at(134).replace(microsecond=900000)
    _check("close", format_close(pos, est, "XAUUSD", now))


def test_refusal_too_small():
    big_min = SymbolSpec(
        symbol="XAUUSD.f", contract_size=100.0, lot_step=0.01, min_lot=1.0,
        max_lot=100.0, digits=2, tick_value=1.0,
    )
    pos = make_position("96484088", direction="sell", volume=0.001,
                        open_price=4400.0, open_time=at(0))
    refusal = size_position(pos, big_min, CFG)
    assert isinstance(refusal, SizingRefused)
    now = at(0).replace(second=1, microsecond=100000)
    _check("refusal_too_small", format_refusal(pos, refusal, "XAUUSD", now))
