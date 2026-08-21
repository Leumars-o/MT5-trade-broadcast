"""RiskEngine: balance-proportional sizing with a risk cap, the ~4pt stop
derived from the 74-trade loss distribution, and fee-drag-aware close estimates."""

from __future__ import annotations

from decimal import Decimal

import pytest

from copier.config import RiskConfig
from copier.core.risk import (
    estimate_close,
    floor_to_step,
    native_lots,
    protective_stop_price,
    signed_points,
    size_position,
)
from copier.models import SizingDecision, SizingRefused, SymbolSpec

from .conftest import at, make_position

# Config matching config.yaml after the 74-trade re-derivation.
CFG = RiskConfig(
    stop_basis_points=3.90,
    buffer_multiplier=1.5,
    native_lots_per_1k=0.00077,
    size_multiplier=1.0,
    commission_per_lot=10.0,
    fee_drag_pct=0.26,
    daily_dd_limit=2500,
    max_dd_limit=5000,
    utilisation_target=0.15,
    destination_equity=50000,
)

XAU = SymbolSpec(
    symbol="XAUUSD.f", contract_size=100.0, lot_step=0.01, min_lot=0.01,
    max_lot=100.0, digits=2, tick_value=1.0, from_fallback=True,
)


def test_floor_to_step_rounds_down():
    assert floor_to_step(0.0385, 0.01) == pytest.approx(0.03)
    assert floor_to_step(0.641, 0.01) == pytest.approx(0.64)


def test_stop_distance_is_worst_loss_times_buffer():
    # 3.90 (p100 realised loss) × 1.5 = 5.85pt synthesised stop.
    pos = make_position("1", direction="sell", open_price=4399.36, open_time=at(0))
    result = size_position(pos, XAU, CFG)
    assert isinstance(result, SizingDecision)
    assert result.stop_distance_points == pytest.approx(5.85)
    assert result.protective_stop_price == pytest.approx(4405.21)  # sell: entry + 5.85


def test_native_sizing_is_balance_proportional():
    """Default 1× reproduces the strategy's own profile: 0.00077 lots/$1k of the
    $50k destination = 0.0385, floored to 0.03. Risk cap does not bind here."""
    assert native_lots(CFG) == pytest.approx(0.0385)
    pos = make_position("1", direction="sell", open_price=4399.36, open_time=at(0))
    result = size_position(pos, XAU, CFG)
    assert isinstance(result, SizingDecision)
    assert result.destination_lots == pytest.approx(0.03)  # rounded DOWN from 0.0385
    assert result.binding_constraint == "proportional"
    assert result.risk_usd == Decimal("17.55")             # 5.85 × 100 × 0.03
    assert result.utilisation_pct == pytest.approx(17.55 / 2500)


def test_risk_cap_binds_when_leveraged():
    """At a high multiplier the proportional target exceeds the cap, so per-trade
    risk is clamped to utilisation_target (15%) of the daily budget = 0.64 lots."""
    cfg = CFG.model_copy(update={"size_multiplier": 20.0})
    pos = make_position("1", direction="sell", open_price=4399.36, open_time=at(0))
    result = size_position(pos, XAU, cfg)
    assert isinstance(result, SizingDecision)
    assert result.destination_lots == pytest.approx(0.64)   # capped, not 0.77
    assert result.binding_constraint == "risk_cap"
    assert result.risk_usd == Decimal("374.4")              # ~15% of 2500
    assert result.utilisation_pct == pytest.approx(0.14976)


def test_risk_mode_sizes_to_utilisation_ladder():
    """In 'risk' mode utilisation is the driver: set the % and get the lots,
    independent of the native proportional size."""
    pos = make_position("1", direction="sell", open_price=4399.36, open_time=at(0))
    for util, expected_lots in [(0.10, 0.42), (0.15, 0.64), (0.25, 1.06), (0.75, 3.20)]:
        cfg = CFG.model_copy(update={"sizing_mode": "risk", "utilisation_target": util})
        result = size_position(pos, XAU, cfg)
        assert isinstance(result, SizingDecision)
        assert result.destination_lots == pytest.approx(expected_lots)
        assert result.binding_constraint == "risk"
        # actual utilisation lands just under the target (lots floored down)
        assert result.utilisation_pct <= util
        assert result.utilisation_pct == pytest.approx(util, abs=0.01)


def test_ceiling_mode_hard_stop_drives_stop_and_size():
    """A 5pt ceiling sets the protective stop AND the risk-per-lot; sizing goes
    to utilisation_target of the daily budget (not stop_basis × buffer)."""
    cfg = CFG.model_copy(update={"sizing_mode": "ceiling", "ceiling_points": 5.0})
    pos = make_position("1", direction="sell", open_price=4399.36, open_time=at(0))
    result = size_position(pos, XAU, cfg)
    assert isinstance(result, SizingDecision)
    assert result.stop_distance_points == pytest.approx(5.0)          # the ceiling
    assert result.protective_stop_price == pytest.approx(4404.36)     # entry + 5
    assert result.destination_lots == pytest.approx(0.75)             # 0.15 util / (5×100)
    assert result.risk_usd == Decimal("375.0")
    assert result.utilisation_pct == pytest.approx(0.15)
    assert result.binding_constraint == "ceiling"


def test_lots_always_rounded_down_never_up():
    pos = make_position("1", direction="sell", open_price=4400.0, open_time=at(0))
    result = size_position(pos, XAU, CFG)
    assert isinstance(result, SizingDecision)
    assert result.destination_lots == pytest.approx(0.03)  # 0.0385 → 0.03, never 0.04


def test_stop_is_above_entry_for_sell_below_for_buy():
    assert protective_stop_price(4400.0, "sell", 5.85, 2) == pytest.approx(4405.85)
    assert protective_stop_price(4400.0, "buy", 5.85, 2) == pytest.approx(4394.15)


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


def test_refuses_when_native_rounds_below_min_lot():
    # A tiny destination equity makes the proportional size round below min_lot.
    cfg = CFG.model_copy(update={"destination_equity": 100.0})
    pos = make_position("1", direction="sell", open_price=4400.0, open_time=at(0))
    result = size_position(pos, XAU, cfg)
    assert isinstance(result, SizingRefused)
    assert "too small" in result.reason
    assert result.protective_stop_price == pytest.approx(4405.85)  # stop still computed


def test_signed_points_matches_direction():
    sell = make_position("1", direction="sell", open_price=4399.36, open_time=at(0))
    assert signed_points(sell.with_updates(close_price=4392.34)) == pytest.approx(7.02)
    buy = make_position("2", direction="buy", open_price=4390.0, open_time=at(0))
    assert signed_points(buy.with_updates(close_price=4392.34)) == pytest.approx(2.34)


def test_estimate_close_is_net_of_commission_and_fee_drag():
    pos = make_position("1", direction="sell", open_price=4399.36, open_time=at(0))
    pos = pos.with_updates(close_price=4392.34, close_time=at(134))
    est = estimate_close(pos, XAU, destination_lots=0.03, cfg=CFG)

    assert est.points == pytest.approx(7.02)
    assert est.gross_usd == Decimal("21.06")        # 7.02 × 100 × 0.03
    assert est.commission_usd == Decimal("0.30")    # 0.03 × 10
    assert est.fee_drag_usd == Decimal("5.4756")    # 26% of gross
    assert est.net_usd == Decimal("15.2844")        # ~¾ of gross, minus commission


def test_no_fee_drag_on_a_losing_trade():
    pos = make_position("1", direction="buy", open_price=4390.0, open_time=at(0))
    pos = pos.with_updates(close_price=4385.0, close_time=at(30))
    est = estimate_close(pos, XAU, destination_lots=0.03, cfg=CFG)
    assert est.gross_usd == Decimal("-15.00")
    assert est.fee_drag_usd == Decimal("0")         # no fee on a loss
    assert est.net_usd == Decimal("-15.30")         # just commission
