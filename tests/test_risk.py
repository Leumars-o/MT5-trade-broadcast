"""RiskEngine: sizing math ported from mt5_risk_audit.py, pinned to the §8
message-contract numbers, plus the refuse-to-size rules."""

from __future__ import annotations

from decimal import Decimal

import pytest

from copier.config import RiskConfig
from copier.core.risk import (
    estimate_close,
    floor_to_step,
    protective_stop_price,
    signed_points,
    size_position,
)
from copier.models import SizingDecision, SizingRefused, SymbolSpec

from .conftest import at, make_position

# Config matching the ARCHITECTURE.md §7 / §8 worked example.
CFG = RiskConfig(
    mae_points=4.64,
    buffer_multiplier=2.0,
    commission_per_lot=10.0,
    daily_dd_limit=2500,
    max_dd_limit=5000,
    utilisation_target=0.15,
    destination_equity=50000,
)

XAU = SymbolSpec(
    symbol="XAUUSD.f",
    contract_size=100.0,
    lot_step=0.01,
    min_lot=0.01,
    max_lot=100.0,
    digits=2,
    tick_value=1.0,
    from_fallback=True,
)


def test_floor_to_step_rounds_down():
    assert floor_to_step(0.4040, 0.01) == pytest.approx(0.40)
    assert floor_to_step(0.409, 0.01) == pytest.approx(0.40)
    assert floor_to_step(0.99, 0.10) == pytest.approx(0.90)


def test_sizes_the_reference_trade():
    """Reproduces the §8 entry example exactly."""
    pos = make_position("96484066", direction="sell", volume=0.001798,
                        open_price=4399.36, open_time=at(0))
    result = size_position(pos, XAU, CFG)

    assert isinstance(result, SizingDecision)
    assert result.destination_lots == pytest.approx(0.40)
    assert result.stop_distance_points == pytest.approx(9.28)
    assert result.protective_stop_price == pytest.approx(4408.64)
    assert result.risk_usd == Decimal("371.2")
    assert result.utilisation_pct == pytest.approx(371.2 / 2500)
    assert result.commission_estimate == Decimal("4.0")
    assert result.mae_points_used == 4.64


def test_lots_always_rounded_down_never_up():
    # Budget/risk_per_lot = 0.4040… must floor to 0.40, never 0.41.
    pos = make_position("1", direction="sell", open_price=4400.0, open_time=at(0))
    result = size_position(pos, XAU, CFG)
    assert isinstance(result, SizingDecision)
    assert result.destination_lots == pytest.approx(0.40)


def test_stop_is_above_entry_for_sell_below_for_buy():
    assert protective_stop_price(4400.0, "sell", 9.28, 2) == pytest.approx(4409.28)
    assert protective_stop_price(4400.0, "buy", 9.28, 2) == pytest.approx(4390.72)


def test_sized_entry_always_has_a_stop():
    for direction in ("buy", "sell"):
        pos = make_position("1", direction=direction, open_price=4400.0, open_time=at(0))
        result = size_position(pos, XAU, CFG)
        assert isinstance(result, SizingDecision)
        assert result.protective_stop_price is not None


def test_refuses_when_spec_unavailable():
    pos = make_position("1", open_time=at(0))
    result = size_position(pos, None, CFG)
    assert isinstance(result, SizingRefused)
    assert "spec unavailable" in result.reason
    assert result.protective_stop_price is None


def test_refuses_when_below_min_lot():
    # A large min_lot forces the sized 0.40 below the floor → refuse, not zero.
    big_min = SymbolSpec(
        symbol="XAUUSD.f", contract_size=100.0, lot_step=0.01, min_lot=1.0,
        max_lot=100.0, digits=2, tick_value=1.0,
    )
    pos = make_position("1", direction="sell", open_price=4400.0, open_time=at(0))
    result = size_position(pos, big_min, CFG)
    assert isinstance(result, SizingRefused)
    assert "too small" in result.reason
    # Stop is still computed even when refusing.
    assert result.protective_stop_price == pytest.approx(4409.28)


def test_signed_points_matches_direction():
    sell = make_position("1", direction="sell", open_price=4399.36, open_time=at(0))
    sell = sell.with_updates(close_price=4392.34)
    assert signed_points(sell) == pytest.approx(7.02)

    buy = make_position("2", direction="buy", open_price=4390.0, open_time=at(0))
    buy = buy.with_updates(close_price=4392.34)
    assert signed_points(buy) == pytest.approx(2.34)


def test_estimate_close_net_of_commission():
    pos = make_position("1", direction="sell", open_price=4399.36, open_time=at(0))
    pos = pos.with_updates(close_price=4392.34, close_time=at(134))
    est = estimate_close(pos, XAU, destination_lots=0.40, cfg=CFG)

    assert est.points == pytest.approx(7.02)
    # gross = 7.02 * 100 * 0.40 = 280.80 ; commission = 0.40 * 10 = 4.00
    assert est.gross_usd == Decimal("280.80")
    assert est.commission_usd == Decimal("4.0")
    assert est.net_usd == Decimal("276.80")
