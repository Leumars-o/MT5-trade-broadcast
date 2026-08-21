"""MetaApiFeed: normalisation, liveness detection, and the terminal_state poll
loop — exercised with a fake connection, no MetaApi network. The whole module
skips if the optional live SDK is not installed."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("metaapi_cloud_sdk")

from copier.feed.metaapi_feed import (  # noqa: E402
    MetaApiFeed,
    normalize_position,
    normalize_spec,
)

_POS = {
    "id": 96484066,
    "type": "POSITION_TYPE_SELL",
    "symbol": "XAUUSD.f",
    "volume": 0.40,
    "openPrice": 4399.36,
    "time": datetime(2025, 8, 18, 10, 5, tzinfo=UTC),
    "stopLoss": 0,
    "takeProfit": 0,
    "profit": 280.8,
}
_SPEC = {
    "symbol": "XAUUSD.f", "contractSize": 100.0, "volumeStep": 0.01,
    "minVolume": 0.01, "maxVolume": 50.0, "digits": 2, "tickSize": 0.01,
}


class _FakeTerminalState:
    def __init__(self, positions, broker=True, spec=None):
        self.positions = positions
        self.connected_to_broker = broker
        self._spec = spec

    def specification(self, symbol):
        return self._spec


class _FakeConn:
    def __init__(self, positions, synced=True, broker=True, spec=None):
        self.synchronized = synced
        self.terminal_state = _FakeTerminalState(positions, broker, spec)


async def _take(feed: MetaApiFeed, n: int, mutate=None):
    """Collect n FeedUpdates from the poll loop, optionally mutating the fake
    connection after each one (to simulate health transitions)."""
    out = []
    async for u in feed.stream():
        out.append(u)
        if mutate is not None:
            mutate(len(out), feed._conn)
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------- normalisation


def test_normalize_position_sell():
    p = normalize_position(_POS)
    assert p.position_id == "96484066"
    assert p.direction == "sell"
    assert p.open_time.tzinfo is not None
    assert p.sl is None and p.tp is None


def test_normalize_position_buy():
    assert normalize_position({**_POS, "type": "POSITION_TYPE_BUY"}).direction == "buy"


def test_normalize_spec_from_broker():
    spec = normalize_spec("XAUUSD.f", _SPEC)
    assert spec.contract_size == pytest.approx(100.0)
    assert spec.digits == 2
    assert spec.from_fallback is False


def test_normalize_spec_fallback_when_missing():
    spec = normalize_spec("XAUUSD.f", None)
    assert spec.from_fallback is True
    assert spec.contract_size == pytest.approx(100.0)


# ---------------------------------------------------------------- read-only guard


def test_read_only_false_is_rejected():
    with pytest.raises(ValueError, match="read-only"):
        MetaApiFeed("token", "acct", read_only=False)


# ---------------------------------------------------------------- liveness


def test_is_healthy_requires_synced_and_broker():
    feed = MetaApiFeed("t", "a")
    feed._conn = _FakeConn([_POS], synced=True, broker=True)
    assert feed._is_healthy() is True
    feed._conn.synchronized = False
    assert feed._is_healthy() is False
    feed._conn.synchronized = True
    feed._conn.terminal_state.connected_to_broker = False
    assert feed._is_healthy() is False


async def test_snapshot_empty_when_unhealthy():
    feed = MetaApiFeed("t", "a")
    feed._conn = _FakeConn([_POS], broker=False)
    # unhealthy → snapshot must not leak stale positions
    assert await feed.snapshot() == []


# ---------------------------------------------------------------- poll loop


async def test_cold_start_poll_is_a_real_diff():
    feed = MetaApiFeed("t", "a", poll_interval=0)
    feed._conn = _FakeConn([_POS])
    updates = await _take(feed, 2)
    # First healthy poll = cold start → resync False so PRE_EXISTING can surface.
    assert updates[0].healthy is True
    assert updates[0].resync is False
    assert [p.position_id for p in updates[0].positions] == ["96484066"]
    assert updates[1].resync is False  # steady state


async def test_unhealthy_poll_is_flagged_and_empty():
    feed = MetaApiFeed("t", "a", poll_interval=0)
    feed._conn = _FakeConn([_POS], broker=False)  # connected socket, broker down
    updates = await _take(feed, 1)
    assert updates[0].healthy is False
    assert updates[0].positions == []
    assert updates[0].resync is False


async def test_recovery_after_gap_is_a_resync():
    """cold start (healthy) → unhealthy gap → recovery poll must be resync=True
    so a momentarily-empty terminal_state cannot phantom-close positions."""
    feed = MetaApiFeed("t", "a", poll_interval=0)
    feed._conn = _FakeConn([_POS], synced=True, broker=True)

    def mutate(i, conn):
        if i == 1:      # after the cold-start poll → simulate a disconnect
            conn.synchronized = False
        elif i == 2:    # after the unhealthy poll → broker back
            conn.synchronized = True

    updates = await _take(feed, 3, mutate)
    assert [u.healthy for u in updates] == [True, False, True]
    assert [u.resync for u in updates] == [False, False, True]  # recovery = resync