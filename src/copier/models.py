"""Core domain models.

All timestamps are timezone-aware UTC datetimes. Prices and points are ``float``;
money is ``Decimal`` (see ``SizingDecision``). These are plain, immutable-ish
dataclasses with no I/O so they stay trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

Direction = Literal["buy", "sell"]


class EventType(StrEnum):
    """The kinds of events the tracker emits.

    ``PRE_EXISTING`` is a distinct, quieter alert for a position that was already
    open before this process started — it must never be reported as ``OPENED``.
    """

    OPENED = "opened"
    MODIFIED = "modified"
    CLOSED = "closed"
    PRE_EXISTING = "pre_existing"


@dataclass(frozen=True, slots=True)
class Position:
    """A single master-account position, normalised from the broker payload."""

    position_id: str
    symbol: str
    direction: Direction
    volume: float
    open_price: float
    open_time: datetime  # aware UTC
    sl: float | None = None
    tp: float | None = None
    close_price: float | None = None
    close_time: datetime | None = None  # aware UTC
    profit: float | None = None

    def with_updates(self, **changes: object) -> Position:
        """Return a copy with fields replaced (positions are frozen)."""
        return replace(self, **changes)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """Broker symbol specification. Fetch from the broker; only fall back to a
    hardcoded value with a logged warning (ARCHITECTURE.md §5.1)."""

    symbol: str
    contract_size: float
    lot_step: float
    min_lot: float
    max_lot: float
    digits: int
    tick_value: float
    from_fallback: bool = False


@dataclass(frozen=True, slots=True)
class PositionEvent:
    """An emitted change in the master's position state.

    ``changed_fields`` is populated for ``MODIFIED`` events; ``volume_delta`` is
    the signed change in volume (negative = partial close).
    """

    event_type: EventType
    position: Position
    detected_at: datetime  # aware UTC
    previous: Position | None = None
    changed_fields: tuple[str, ...] = ()
    volume_delta: float | None = None

    @property
    def position_id(self) -> str:
        return self.position.position_id

    @property
    def is_partial_close(self) -> bool:
        return (
            self.event_type is EventType.MODIFIED
            and self.volume_delta is not None
            and self.volume_delta < 0
        )


class GovernorVerdict(StrEnum):
    OK = "ok"
    WARN = "warn"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class SizingDecision:
    """Output of the risk engine (implemented in M2). Money is ``Decimal``."""

    destination_lots: float
    protective_stop_price: float
    risk_usd: Decimal
    utilisation_pct: float
    commission_estimate: Decimal
    mae_points_used: float
    buffer_multiplier: float
    governor_verdict: GovernorVerdict = GovernorVerdict.OK
    notes: tuple[str, ...] = field(default_factory=tuple)
