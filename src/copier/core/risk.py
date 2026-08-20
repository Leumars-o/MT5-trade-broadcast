"""RiskEngine — position sizing, the synthesised protective stop, and the
destination-side close estimate. Ported from ``mt5_risk_audit.py``.

Pure functions only (no I/O, no clock reads) so every decision is deterministic
and unit-testable. The wiring layer is responsible for *logging* which
``mae_points`` value was used (exposed on the result) — the engine never logs.

Hard rules (ARCHITECTURE.md §5.3, §10):

* Lots are **always rounded DOWN** to ``lot_step``. Rounding up silently
  breaches the daily budget.
* Every sized entry carries a **protective_stop_price** — no exceptions.
* If the spec is missing, or the sized lots fall below ``min_lot``, the engine
  **refuses** (returns ``SizingRefused``) rather than sizing something arbitrary.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from ..config import RiskConfig
from ..models import (
    BindingConstraint,
    CloseEstimate,
    Direction,
    Position,
    SizingDecision,
    SizingRefused,
    SizingResult,
    SymbolSpec,
)


def _d(value: object) -> Decimal:
    """Decimal via str to avoid binary float artefacts in money maths."""
    return Decimal(str(value))


def floor_to_step(value: float, step: float) -> float:
    """Round ``value`` DOWN to the nearest multiple of ``step``."""
    quantised = (_d(value) / _d(step)).to_integral_value(rounding=ROUND_FLOOR)
    return float(quantised * _d(step))


def _direction_sign(direction: Direction) -> int:
    """+1 for buy, -1 for sell — the sign of a favourable price move."""
    return 1 if direction == "buy" else -1


def protective_stop_price(
    entry_price: float, direction: Direction, stop_distance_points: float, digits: int
) -> float:
    """The synthesised stop: below entry for a buy, above entry for a sell.

    The master sets no stop, so this is *our* protective distance, not a copy of
    the master's risk management (ARCHITECTURE.md §1.2)."""
    # Adverse direction is opposite the favourable one.
    price = entry_price - _direction_sign(direction) * stop_distance_points
    return round(price, digits)


def native_lots(cfg: RiskConfig) -> float:
    """The strategy's own size on the destination equity (its native 1× profile),
    before rounding or the risk cap. This is the balance-proportional target."""
    return cfg.native_lots_per_1k * (cfg.destination_equity / 1000.0) * cfg.size_multiplier


def size_position(
    position: Position, spec: SymbolSpec | None, cfg: RiskConfig
) -> SizingResult:
    """Size a destination position: balance-proportional, capped by risk.

    The master is balance-proportional (a constant lots-per-$1k), so the natural
    copy is that same rate on the destination equity (``native_lots``). We then
    clamp so per-trade risk never exceeds ``utilisation_target`` of the daily
    budget. Whichever limit bounds is recorded on the decision. Returns a
    ``SizingDecision`` when copyable, else a ``SizingRefused``.
    """
    stop_distance = cfg.stop_basis_points * cfg.buffer_multiplier

    if spec is None:
        return SizingRefused(
            reason="symbol spec unavailable — cannot size safely",
            raw_lots=None,
            min_lot=None,
            protective_stop_price=None,
            stop_basis_points=cfg.stop_basis_points,
            buffer_multiplier=cfg.buffer_multiplier,
        )

    stop_price = protective_stop_price(
        position.open_price, position.direction, stop_distance, spec.digits
    )

    target = native_lots(cfg)
    risk_per_lot = stop_distance * spec.contract_size  # $ lost per lot if stop hit
    cap_budget = cfg.daily_dd_limit * cfg.utilisation_target
    cap_lots = cap_budget / risk_per_lot if risk_per_lot > 0 else 0.0

    binding: BindingConstraint = "risk_cap" if cap_lots < target else "proportional"
    raw_lots = min(target, cap_lots)
    lots = floor_to_step(raw_lots, spec.lot_step)  # always DOWN

    if lots < spec.min_lot:
        return SizingRefused(
            reason=(
                f"too small to copy safely: native size {target:g} lots rounds "
                f"below min_lot {spec.min_lot:g}"
            ),
            raw_lots=raw_lots,
            min_lot=spec.min_lot,
            protective_stop_price=stop_price,
            stop_basis_points=cfg.stop_basis_points,
            buffer_multiplier=cfg.buffer_multiplier,
        )

    risk_usd = _d(stop_distance) * _d(spec.contract_size) * _d(lots)
    commission = _d(lots) * _d(cfg.commission_per_lot)
    utilisation_pct = float(risk_usd) / cfg.daily_dd_limit if cfg.daily_dd_limit else 0.0

    return SizingDecision(
        destination_lots=lots,
        protective_stop_price=stop_price,
        stop_distance_points=stop_distance,
        risk_usd=risk_usd,
        utilisation_pct=utilisation_pct,
        commission_estimate=commission,
        stop_basis_points=cfg.stop_basis_points,
        buffer_multiplier=cfg.buffer_multiplier,
        native_lots=target,
        size_multiplier=cfg.size_multiplier,
        binding_constraint=binding,
    )


def _signed_points_decimal(position: Position) -> Decimal:
    """Signed price move as a ``Decimal`` — the subtraction is done in Decimal so
    float artefacts (e.g. 4392.34 - 4399.36) never leak into money figures."""
    if position.close_price is None:
        return Decimal("0")
    return (_d(position.close_price) - _d(position.open_price)) * _direction_sign(
        position.direction
    )


def signed_points(position: Position) -> float:
    """Signed price move captured by the position, in points (price units).

    Positive = favourable. Mirrors ``mt5_risk_audit.py``'s ``points`` column.
    """
    return float(_signed_points_decimal(position))


def estimate_close(
    position: Position, spec: SymbolSpec, destination_lots: float, cfg: RiskConfig
) -> CloseEstimate:
    """Estimate the destination-side P&L of a closed master trade, using the
    lots the engine sized at entry."""
    points = _signed_points_decimal(position)
    gross = points * _d(spec.contract_size) * _d(destination_lots)
    commission = _d(destination_lots) * _d(cfg.commission_per_lot)
    # Prop-firm fee drag applies to gross *profit* only (no fee on a loss).
    fee_drag = _d(cfg.fee_drag_pct) * gross if gross > 0 else Decimal("0")
    return CloseEstimate(
        points=float(points),
        gross_usd=gross,
        commission_usd=commission,
        fee_drag_usd=fee_drag,
        net_usd=gross - commission - fee_drag,
        destination_lots=destination_lots,
    )
