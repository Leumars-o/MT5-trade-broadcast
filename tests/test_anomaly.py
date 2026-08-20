"""AnomalyDetector tripwires (§5.5): volume step, direction/P&L mismatch,
contract-size drift, and over-hold."""

from __future__ import annotations

from copier.core.anomaly import AnomalyDetector
from copier.models import AnomalyKind, SymbolSpec

from .conftest import at, make_position

XAU = SymbolSpec(
    symbol="XAUUSD.f", contract_size=100.0, lot_step=0.01, min_lot=0.01,
    max_lot=100.0, digits=2, tick_value=1.0,
)


def _det() -> AnomalyDetector:
    return AnomalyDetector(volume_step_factor=2.0, drift_tolerance=0.02, max_hold_minutes=240)


# ---------------------------------------------------------------- volume step


def test_volume_step_fires_on_doubling():
    d = _det()
    assert d.on_open(make_position("1", volume=0.000899)) is None  # first: baseline
    a = d.on_open(make_position("2", volume=0.001798))            # 2×
    assert a is not None and a.kind is AnomalyKind.VOLUME_STEP
    assert a.ratio == 2.0
    assert a.consecutive == 1


def test_normal_volume_change_does_not_fire():
    d = _det()
    d.on_open(make_position("1", volume=0.001))
    assert d.on_open(make_position("2", volume=0.0012)) is None  # 1.2× < 2×


def test_consecutive_steps_counted_and_reset():
    d = _det()
    d.on_open(make_position("1", volume=0.001))
    assert d.on_open(make_position("2", volume=0.002)).consecutive == 1
    assert d.on_open(make_position("3", volume=0.004)).consecutive == 2
    assert d.on_open(make_position("4", volume=0.0045)) is None   # 1.125× resets
    assert d.on_open(make_position("5", volume=0.009)).consecutive == 1


# ---------------------------------------------------------------- direction / P&L


def test_direction_pnl_mismatch_fires():
    d = _det()
    # A sell that moved favourably (price fell) but shows a LOSS → mismatch.
    pos = make_position("1", direction="sell", open_price=4400.0)
    pos = pos.with_updates(close_price=4390.0, profit=-50.0)  # +10 pts but -$50
    kinds = [a.kind for a in d.on_close(pos, XAU)]
    assert AnomalyKind.DIRECTION_PNL in kinds


def test_consistent_direction_pnl_does_not_fire():
    d = _det()
    pos = make_position("1", direction="sell", open_price=4400.0)
    pos = pos.with_updates(close_price=4390.0, profit=30.0)  # +10 pts, +profit
    assert all(a.kind is not AnomalyKind.DIRECTION_PNL for a in d.on_close(pos, XAU))


# ---------------------------------------------------------------- contract drift


def test_contract_drift_fires_when_implied_far_from_spec():
    d = _det()
    pos = make_position("1", direction="buy", open_price=4400.0, volume=0.10)
    # +10 pts × 0.10 lots × 100 = $100 expected; report $130 → implied 130 ≠ 100.
    pos = pos.with_updates(close_price=4410.0, profit=130.0)
    kinds = [a.kind for a in d.on_close(pos, XAU)]
    assert AnomalyKind.CONTRACT_DRIFT in kinds


def test_no_contract_drift_within_tolerance():
    d = _det()
    pos = make_position("1", direction="buy", open_price=4400.0, volume=0.10)
    pos = pos.with_updates(close_price=4410.0, profit=100.0)  # exactly 100 oz/lot
    assert all(a.kind is not AnomalyKind.CONTRACT_DRIFT for a in d.on_close(pos, XAU))


# ---------------------------------------------------------------- over-hold


def test_over_hold_fires_once_then_rearms():
    d = _det()
    pos = make_position("1", open_time=at(0))
    assert d.check_holds([pos], now=at(200)) == []          # under 240m
    hits = d.check_holds([pos], now=at(300))                # 300m > 240m
    assert [a.kind for a in hits] == [AnomalyKind.OVER_HOLD]
    assert hits[0].held_minutes == 300
    assert d.check_holds([pos], now=at(360)) == []          # already flagged
    # Position closes (absent), then a new position with the same id re-arms.
    assert d.check_holds([], now=at(400)) == []
    assert len(d.check_holds([pos], now=at(700))) == 1
