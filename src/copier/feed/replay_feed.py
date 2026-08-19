"""ReplayFeed — reconstructs the position snapshots that would have produced an
MT5 "Trade History Report" (.xlsx) export, driven by a fake clock.

This is what makes the whole system testable without a live account (a v1
deliverable, not a stretch goal — ARCHITECTURE.md §5.1). It parses the Positions
block the same way ``mt5_risk_audit.py`` does, then walks the open/close event
boundaries emitting one snapshot per boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..models import Direction, Position, SymbolSpec
from ..timeutil import to_utc
from .base import FeedUpdate

# Fallback contract sizes (oz per standard lot). Live feeds must fetch these from
# the broker; here they only populate SymbolSpec for replayed data.
_CONTRACT_FALLBACK = {"XAUUSD": 100.0, "XAGUSD": 5000.0}


def _strip_suffix(symbol: str) -> str:
    """XAUUSD.f -> XAUUSD"""
    return str(symbol).split(".")[0].upper()


def parse_positions(path: str | Path, server_tz: str | None = None) -> list[Position]:
    """Parse the Positions block of an MT5 report into ``Position`` objects.

    Blocks are located by their header rows so this survives MT5 adding or
    removing rows between versions (mirrors ``mt5_risk_audit.parse_mt5_report``).
    Report times are naive server-local; ``server_tz`` converts them to UTC.
    """
    raw = pd.read_excel(path, sheet_name=0, header=None)
    col0 = raw[0].astype(str)

    try:
        start = col0[col0 == "Positions"].index[0] + 2  # skip label + header
    except IndexError as exc:  # pragma: no cover - defensive
        raise ValueError("No 'Positions' block found — not an MT5 history report?") from exc
    end_hits = col0[col0.isin(["Orders", "Deals"])]
    later = end_hits.index[end_hits.index > start]
    end = later[0] if len(later) else len(raw)

    # Column count varies between MT5 versions (some export a trailing spacer
    # column). Map by position up to the block's real width.
    names = [
        "open_time", "position", "symbol", "type", "volume", "open_price",
        "sl", "tp", "close_time", "close_price", "commission", "swap",
        "profit", "_x",
    ]
    width = min(len(names), raw.shape[1])
    block = raw.iloc[start:end, :width].copy()
    block.columns = names[:width]
    block = block.dropna(subset=["symbol"])

    for c in ("volume", "open_price", "sl", "tp", "close_price", "profit"):
        block[c] = pd.to_numeric(block[c], errors="coerce")
    for c in ("open_time", "close_time"):
        block[c] = pd.to_datetime(block[c], format="%Y.%m.%d %H:%M:%S", errors="coerce")

    positions: list[Position] = []
    for _, r in block.iterrows():
        raw_type = str(r["type"]).lower().strip()
        if raw_type not in ("buy", "sell"):
            continue
        direction: Direction = raw_type  # type: ignore[assignment]
        open_dt = _coerce_dt(r["open_time"], server_tz)
        if open_dt is None:
            continue
        positions.append(
            Position(
                position_id=str(r["position"]).split(".")[0],
                symbol=str(r["symbol"]),
                direction=direction,
                volume=float(r["volume"]),
                open_price=float(r["open_price"]),
                open_time=open_dt,
                sl=_opt_float(r["sl"]),
                tp=_opt_float(r["tp"]),
                close_price=_opt_float(r["close_price"]),
                close_time=_coerce_dt(r["close_time"], server_tz),
                profit=_opt_float(r["profit"]),
            )
        )
    return positions


def _opt_float(v: object) -> float | None:
    f = pd.to_numeric(v, errors="coerce")
    if pd.isna(f) or f == 0:
        return None
    return float(f)


def _coerce_dt(v: object, server_tz: str | None) -> datetime | None:
    if v is None or pd.isna(v):
        return None
    return to_utc(pd.Timestamp(v).to_pydatetime(), assume_tz=server_tz)


def build_snapshots(
    positions: list[Position],
) -> list[tuple[datetime, list[Position]]]:
    """Turn a set of positions into the timeline of snapshots that produced them.

    A position is open over ``[open_time, close_time)``. The boundaries are the
    sorted, de-duplicated set of all open and close times; each boundary yields
    the set of positions open at that instant. A position closing exactly at a
    boundary is excluded there, so the tracker sees it disappear (→ CLOSED).
    """
    boundaries: set[datetime] = set()
    for p in positions:
        boundaries.add(p.open_time)
        if p.close_time is not None:
            boundaries.add(p.close_time)

    snapshots: list[tuple[datetime, list[Position]]] = []
    for t in sorted(boundaries):
        open_now = [
            p
            for p in positions
            if p.open_time <= t and (p.close_time is None or p.close_time > t)
        ]
        snapshots.append((t, open_now))
    return snapshots


class ReplayFeed:
    """A ``PositionFeed`` backed by a parsed MT5 report (no network)."""

    def __init__(
        self,
        path: str | Path,
        server_tz: str | None = None,
        step_delay: float = 0.0,
    ) -> None:
        self._path = Path(path)
        self._server_tz = server_tz
        self._step_delay = step_delay
        self._positions: list[Position] = []
        self._snapshots: list[tuple[datetime, list[Position]]] = []
        self._cursor = -1

    async def connect(self) -> None:
        self._positions = parse_positions(self._path, self._server_tz)
        self._snapshots = build_snapshots(self._positions)
        self._cursor = -1

    @property
    def snapshots(self) -> list[tuple[datetime, list[Position]]]:
        return list(self._snapshots)

    async def snapshot(self) -> list[Position]:
        """The current snapshot (last one emitted), or the final one if idle."""
        if not self._snapshots:
            return []
        idx = self._cursor if self._cursor >= 0 else len(self._snapshots) - 1
        return list(self._snapshots[idx][1])

    async def stream(self) -> AsyncIterator[FeedUpdate]:
        """Yield one ``FeedUpdate`` per event boundary in report order."""
        for i, (server_time, positions) in enumerate(self._snapshots):
            self._cursor = i
            yield FeedUpdate(list(positions), server_time=server_time)
            if self._step_delay:
                await asyncio.sleep(self._step_delay)

    async def symbol_spec(self, symbol: str) -> SymbolSpec:
        root = _strip_suffix(symbol)
        contract = _CONTRACT_FALLBACK.get(root, 100.0)
        return SymbolSpec(
            symbol=symbol,
            contract_size=contract,
            lot_step=0.01,
            min_lot=0.01,
            max_lot=100.0,
            digits=2,
            tick_value=contract * 0.01,
            from_fallback=True,
        )
