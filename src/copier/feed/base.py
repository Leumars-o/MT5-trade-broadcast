"""PositionFeed protocol — the single seam between broker access and the rest
of the system. Implemented twice: ``ReplayFeed`` (tests, M1) and
``MetaApiFeed`` (live, read-only, M4). Nothing downstream knows which is in use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..models import Position, SymbolSpec


@runtime_checkable
class PositionFeed(Protocol):
    async def connect(self) -> None:
        """Establish the (read-only) connection. Idempotent."""
        ...

    async def snapshot(self) -> list[Position]:
        """Return the current set of open positions."""
        ...

    def stream(self) -> AsyncIterator[FeedUpdate]:
        """Yield updates as positions change. Each update carries the snapshot
        plus flags the tracker needs (e.g. ``resync`` after a reconnect)."""
        ...

    async def symbol_spec(self, symbol: str) -> SymbolSpec:
        """Broker symbol specification. Live feeds fetch this; do not hardcode
        contract size (ARCHITECTURE.md §5.1)."""
        ...


class FeedUpdate:
    """One streamed snapshot plus metadata.

    ``resync`` marks the first snapshot after a (re)connect: the tracker must
    treat it as a state reconciliation, not a diff source, to avoid phantom
    CLOSED events. ``server_time`` is the broker event time for this snapshot.
    """

    __slots__ = ("positions", "resync", "server_time")

    def __init__(
        self,
        positions: list[Position],
        *,
        resync: bool = False,
        server_time: datetime | None = None,
    ) -> None:
        self.positions = positions
        self.resync = resync
        self.server_time = server_time

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"FeedUpdate(n={len(self.positions)}, resync={self.resync}, "
            f"server_time={self.server_time})"
        )
