"""Shared test helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from copier.models import Direction, Position

BASE = datetime(2025, 8, 18, 9, 0, 0, tzinfo=UTC)


def at(minutes: int = 0) -> datetime:
    """A UTC datetime ``minutes`` after the base clock."""
    return BASE + timedelta(minutes=minutes)


def make_position(
    position_id: str = "1",
    *,
    direction: Direction = "sell",
    symbol: str = "XAUUSD.f",
    volume: float = 0.10,
    open_price: float = 4400.0,
    open_time: datetime | None = None,
    sl: float | None = None,
    tp: float | None = None,
) -> Position:
    return Position(
        position_id=position_id,
        symbol=symbol,
        direction=direction,
        volume=volume,
        open_price=open_price,
        open_time=open_time or BASE,
        sl=sl,
        tp=tp,
    )
