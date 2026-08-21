"""MetaApiFeed — live, READ-ONLY position feed (ARCHITECTURE.md M4).

Connects to the master MT5 account through MetaApi's cloud using **investor
(read-only) credentials**. It **polls the SDK's authoritative ``terminal_state``
every ``poll_interval`` seconds** rather than trusting the synchronisation event
callbacks. The event-driven approach fails silently: after a reconnect MetaApi
fires one ``on_positions_synchronized`` and then, in a calm market, nothing —
so the stream goes quiet, the dead-feed watchdog false-fires, and a trade that
opens and closes between events is missed entirely (observed in the field).

Polling ``terminal_state`` fixes all of that: a snapshot every ~2s catches a
position open/close within the interval, and liveness is judged by
``synchronized && connected_to_broker`` (the real signal) rather than "did a
position change recently". Each poll carries a ``healthy`` flag; the watchdog
tracks the last *healthy* poll, so a quiet market never looks stale.

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
from collections.abc import AsyncIterator
from typing import Any

from metaapi_cloud_sdk import MetaApi

from ..logging_config import get_logger
from ..models import Direction, Position, SymbolSpec
from ..timeutil import to_utc
from .base import FeedUpdate

log = get_logger("copier.feed.metaapi")

# Fallback contract sizes (oz per standard lot) if the broker spec is missing.
_CONTRACT_FALLBACK = {"XAUUSD": 100.0, "XAGUSD": 5000.0}


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


class MetaApiFeed:
    """A live ``PositionFeed`` backed by MetaApi, polling ``terminal_state``."""

    def __init__(
        self,
        token: str,
        account_id: str,
        *,
        read_only: bool = True,
        api: Any | None = None,
        poll_interval: float = 2.0,
    ) -> None:
        if not read_only:
            # There is no code path that trades; refusing here documents intent
            # and stops a misconfigured run before it connects.
            raise ValueError("MetaApiFeed is read-only; read_only=False is not supported")
        self._token = token
        self._account_id = account_id
        self._api = api
        self._poll_interval = poll_interval
        self._conn: Any | None = None
        self._closed = False

    async def connect(self) -> None:
        log.info("metaapi_connecting", account_id=self._account_id, mode="read_only")
        api = self._api or MetaApi(self._token)
        account = await api.metatrader_account_api.get_account(self._account_id)
        await account.wait_connected()

        conn = account.get_streaming_connection()
        await conn.connect()
        await conn.wait_synchronized()
        self._conn = conn
        log.info("metaapi_connected", account_id=self._account_id)

    def _is_healthy(self) -> bool:
        """True only when the SDK is synchronised AND the broker terminal is
        connected — the real liveness signal, independent of trade activity."""
        conn = self._conn
        if conn is None:
            return False
        try:
            return bool(conn.synchronized and conn.terminal_state.connected_to_broker)
        except Exception:  # noqa: BLE001 - any SDK state error means "not healthy"
            return False

    def _positions(self) -> list[Position]:
        if self._conn is None:
            return []
        return [normalize_position(p) for p in self._conn.terminal_state.positions]

    async def snapshot(self) -> list[Position]:
        return self._positions() if self._is_healthy() else []

    async def stream(self) -> AsyncIterator[FeedUpdate]:
        """Poll ``terminal_state`` every ``poll_interval``. The first *healthy*
        poll is a cold start (surfaces PRE_EXISTING); the first healthy poll
        after any unhealthy gap is a silent resync (avoids phantom CLOSEDs)."""
        first_ever = True
        prev_healthy = False
        while not self._closed:
            healthy = self._is_healthy()
            if healthy:
                positions = self._positions()
                if first_ever:
                    resync = False  # cold start → PRE_EXISTING surfaces (§5.2)
                    first_ever = False
                else:
                    resync = not prev_healthy  # recovery poll after a gap → merge
            else:
                positions = []
                resync = False
            yield FeedUpdate(positions, resync=resync, healthy=healthy)
            prev_healthy = healthy
            await asyncio.sleep(self._poll_interval)

    async def symbol_spec(self, symbol: str) -> SymbolSpec:
        spec = None
        if self._conn is not None:
            spec = self._conn.terminal_state.specification(symbol)
        return normalize_spec(symbol, spec)

    async def close(self) -> None:
        self._closed = True
        if self._conn is not None:
            await self._conn.close()
