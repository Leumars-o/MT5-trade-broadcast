"""MetaApiFeed — live, READ-ONLY streaming position feed (ARCHITECTURE.md M4).

Connects to the master MT5 account through MetaApi's cloud using **investor
(read-only) credentials** and streams position snapshots over a websocket. A
``SynchronizationListener`` turns MetaApi's incremental events into full
snapshots (read from the authoritative ``terminal_state``), which flow through
the exact same ``FeedUpdate`` contract as ``ReplayFeed``.

Read-only is enforced structurally, not by trusting this file:
  1. The account is provisioned with the **investor password** — the broker
     itself rejects any order.
  2. This module calls only connect / synchronise / read / close methods. It
     never references a trading method, so ``test_no_order_placement`` stays
     green. Do not add one here.

The SDK is an optional dependency (``pip install -e ".[live]"``); this module is
imported only on the live path so the default test suite needs no broker.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any

from metaapi_cloud_sdk import MetaApi, SynchronizationListener

from ..logging_config import get_logger
from ..models import Direction, Position, SymbolSpec
from ..timeutil import to_utc, utcnow
from .base import FeedUpdate

log = get_logger("copier.feed.metaapi")

# Fallback contract sizes (oz per standard lot) if the broker spec is missing.
_CONTRACT_FALLBACK = {"XAUUSD": 100.0, "XAGUSD": 5000.0}
_STREAM_END = object()  # sentinel to terminate the stream


def _strip_suffix(symbol: str) -> str:
    return str(symbol).split(".")[0].upper()


def normalize_position(p: dict[str, Any]) -> Position:
    """Normalise a MetaApi position dict into our ``Position``.

    Only open positions appear in ``terminal_state.positions``; the master sets
    no stop, so ``sl``/``tp`` are usually absent. ``close_*`` are never present
    here — a close is inferred by the tracker when the id disappears.
    """
    direction: Direction = "buy" if p["type"] == "POSITION_TYPE_BUY" else "sell"
    return Position(
        position_id=str(p["id"]),
        symbol=str(p["symbol"]),
        direction=direction,
        volume=float(p["volume"]),
        open_price=float(p["openPrice"]),
        open_time=to_utc(p["time"]),
        sl=_opt(p.get("stopLoss")),
        tp=_opt(p.get("takeProfit")),
        profit=_opt(p.get("profit")),
    )


def _opt(value: Any) -> float | None:
    if value is None or value == 0:
        return None
    return float(value)


def normalize_spec(
    symbol: str, spec: dict[str, Any] | None, fallback_contract: float | None = None
) -> SymbolSpec:
    """Normalise a MetaApi symbol specification, falling back to a hardcoded
    contract size (with a logged warning) only when the broker spec is missing."""
    if spec is None:
        contract = fallback_contract or _CONTRACT_FALLBACK.get(_strip_suffix(symbol), 100.0)
        log.warning("symbol_spec_fallback", symbol=symbol, contract_size=contract)
        return SymbolSpec(
            symbol=symbol, contract_size=contract, lot_step=0.01, min_lot=0.01,
            max_lot=100.0, digits=2, tick_value=contract * 0.01, from_fallback=True,
        )
    contract = float(spec["contractSize"])
    tick_size = float(spec.get("tickSize", 0.0))
    return SymbolSpec(
        symbol=symbol,
        contract_size=contract,
        lot_step=float(spec["volumeStep"]),
        min_lot=float(spec["minVolume"]),
        max_lot=float(spec["maxVolume"]),
        digits=int(spec["digits"]),
        tick_value=contract * tick_size,
        from_fallback=False,
    )


class _FeedListener(SynchronizationListener):
    """Bridges MetaApi synchronisation callbacks to the feed's snapshot queue.

    Every relevant event triggers a full snapshot read from ``terminal_state``.
    ``on_positions_synchronized`` (the end of the initial/reconnect sync) is
    flagged ``resync=True`` so the tracker treats it as reconciliation, never a
    source of phantom CLOSED events.
    """

    def __init__(self, feed: MetaApiFeed) -> None:
        super().__init__()
        self._feed = feed

    async def on_positions_synchronized(self, instance_index: str, synchronization_id: str) -> None:
        await self._feed._connection_change(True)  # a no-op on the first sync
        await self._feed._emit(resync=True)

    async def on_positions_updated(
        self, instance_index: str, positions: Any, removed_position_ids: Any
    ) -> None:
        await self._feed._emit(resync=False)

    async def on_position_updated(self, instance_index: str, position: Any) -> None:
        await self._feed._emit(resync=False)

    async def on_position_removed(self, instance_index: str, position_id: str) -> None:
        await self._feed._emit(resync=False)

    async def on_positions_replaced(self, instance_index: str, positions: Any) -> None:
        await self._feed._emit(resync=False)

    async def on_disconnected(self, instance_index: str) -> None:
        log.warning("metaapi_disconnected", instance=instance_index)
        await self._feed._connection_change(False)


class MetaApiFeed:
    """A live ``PositionFeed`` backed by MetaApi streaming (read-only)."""

    def __init__(
        self,
        token: str,
        account_id: str,
        *,
        read_only: bool = True,
        api: Any | None = None,
        on_connection_change: Callable[[bool, datetime], Awaitable[None]] | None = None,
    ) -> None:
        if not read_only:
            # There is no code path that trades; refusing here documents intent
            # and stops a misconfigured run before it connects.
            raise ValueError("MetaApiFeed is read-only; read_only=False is not supported")
        self._token = token
        self._account_id = account_id
        self._api = api
        self._on_connection_change = on_connection_change
        self._conn: Any | None = None
        self._queue: asyncio.Queue[FeedUpdate | object] = asyncio.Queue()

    async def _connection_change(self, connected: bool) -> None:
        if self._on_connection_change is not None:
            await self._on_connection_change(connected, utcnow())

    async def connect(self) -> None:
        log.info("metaapi_connecting", account_id=self._account_id, mode="read_only")
        api = self._api or MetaApi(self._token)
        account = await api.metatrader_account_api.get_account(self._account_id)
        await account.wait_connected()

        conn = account.get_streaming_connection()
        conn.add_synchronization_listener(_FeedListener(self))
        await conn.connect()
        await conn.wait_synchronized()
        self._conn = conn
        log.info("metaapi_connected", account_id=self._account_id)

    async def _emit(self, *, resync: bool) -> None:
        """Read the authoritative position set and enqueue it as a snapshot."""
        if self._conn is None:
            return
        positions = [normalize_position(p) for p in self._conn.terminal_state.positions]
        await self._queue.put(FeedUpdate(positions, resync=resync, server_time=utcnow()))

    async def snapshot(self) -> list[Position]:
        if self._conn is None:
            return []
        return [normalize_position(p) for p in self._conn.terminal_state.positions]

    async def stream(self) -> AsyncIterator[FeedUpdate]:
        while True:
            item = await self._queue.get()
            if item is _STREAM_END:
                return
            assert isinstance(item, FeedUpdate)
            yield item

    async def symbol_spec(self, symbol: str) -> SymbolSpec:
        spec = None
        if self._conn is not None:
            spec = self._conn.terminal_state.specification(symbol)
        return normalize_spec(symbol, spec)

    async def close(self) -> None:
        await self._queue.put(_STREAM_END)
        if self._conn is not None:
            await self._conn.close()
