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


BindingConstraint = Literal["proportional", "risk_cap"]


@dataclass(frozen=True, slots=True)
class SizingDecision:
    """A sized entry from the risk engine. Money is ``Decimal``.

    Sizing is **balance-proportional with a risk cap** (ARCHITECTURE.md §5.3):
    the strategy's own native size (``native_lots``) scaled by ``size_multiplier``,
    then clamped so per-trade risk never exceeds ``utilisation_target`` of the
    daily budget. ``binding_constraint`` records which limit actually bound.

    Always carries a ``protective_stop_price`` — there is no code path that
    produces an entry alert without one (§5.3, §10.4).
    """

    destination_lots: float
    protective_stop_price: float
    stop_distance_points: float
    risk_usd: Decimal
    utilisation_pct: float
    commission_estimate: Decimal
    stop_basis_points: float          # worst realised loss (p100), NOT floating MAE
    buffer_multiplier: float
    native_lots: float                # strategy's own size on destination equity
    size_multiplier: float            # multiple of native this decision targets
    binding_constraint: BindingConstraint
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SizingRefused:
    """The risk engine declined to size a trade — a too-small position or a
    missing symbol spec. Surfaced as a quieter INFO alert, never a silent zero
    (ARCHITECTURE.md §5.3). A protective stop is still computed when possible."""

    reason: str
    raw_lots: float | None
    min_lot: float | None
    protective_stop_price: float | None
    stop_basis_points: float
    buffer_multiplier: float


SizingResult = SizingDecision | SizingRefused


@dataclass(frozen=True, slots=True)
class CloseEstimate:
    """Destination-side estimate of a closed trade's outcome, using the lots the
    engine sized at entry. Money is ``Decimal``.

    ``net_usd`` is after both spread/commission and the modelled prop-firm fee
    drag (~26% of gross on the 74-trade sample) — net runs ~¾ of gross.
    """

    points: float
    gross_usd: Decimal
    commission_usd: Decimal
    fee_drag_usd: Decimal
    net_usd: Decimal
    destination_lots: float
