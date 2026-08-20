"""MetaApiFeed: normalisation and the listener→snapshot bridge, exercised with
fake position/spec data and a fake connection — no MetaApi network. The whole
module skips if the optional live SDK is not installed."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("metaapi_cloud_sdk")

from copier.feed.base import FeedUpdate  # noqa: E402
from copier.feed.metaapi_feed import (  # noqa: E402
    MetaApiFeed,
    _FeedListener,
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
    def __init__(self, positions, spec=None):
        self.positions = positions
        self._spec = spec

    def specification(self, symbol):
        return self._spec


class _FakeConn:
    def __init__(self, positions, spec=None):
        self.terminal_state = _FakeTerminalState(positions, spec)


# ---------------------------------------------------------------- normalisation


def test_normalize_position_sell():
    p = normalize_position(_POS)
    assert p.position_id == "96484066"
    assert p.direction == "sell"
    assert p.volume == pytest.approx(0.40)
    assert p.open_price == pytest.approx(4399.36)
    assert p.open_time.tzinfo is not None
    assert p.sl is None and p.tp is None  # zero stop/target normalise to None


def test_normalize_position_buy():
    p = normalize_position({**_POS, "type": "POSITION_TYPE_BUY"})
    assert p.direction == "buy"


def test_normalize_spec_from_broker():
    spec = normalize_spec("XAUUSD.f", _SPEC)
    assert spec.contract_size == pytest.approx(100.0)
    assert spec.lot_step == pytest.approx(0.01)
    assert spec.min_lot == pytest.approx(0.01)
    assert spec.digits == 2
    assert spec.from_fallback is False


def test_normalize_spec_fallback_when_missing():
    spec = normalize_spec("XAUUSD.f", None)
    assert spec.from_fallback is True
    assert spec.contract_size == pytest.approx(100.0)  # from fallback table


# ---------------------------------------------------------------- read-only guard


def test_read_only_false_is_rejected():
    with pytest.raises(ValueError, match="read-only"):
        MetaApiFeed("token", "acct", read_only=False)


# ---------------------------------------------------------------- listener bridge


async def _drain(feed: MetaApiFeed) -> list[FeedUpdate]:
    out = []
    while not feed._queue.empty():
        out.append(await feed._queue.get())
    return out


async def test_first_sync_is_a_real_diff_later_syncs_are_resync():
    """Cold start must surface already-open positions (PRE_EXISTING, §5.2), so the
    first sync is resync=False; every subsequent sync is a reconnect resync."""
    feed = MetaApiFeed("token", "acct")
    feed._conn = _FakeConn([_POS])
    listener = _FeedListener(feed)

    await listener.on_positions_synchronized("0", "sync-1")  # cold start
    await listener.on_positions_synchronized("0", "sync-2")  # reconnect

    updates = await _drain(feed)
    assert [u.resync for u in updates] == [False, True]
    assert [p.position_id for p in updates[0].positions] == ["96484066"]


async def test_incremental_updates_are_not_resync():
    feed = MetaApiFeed("token", "acct")
    feed._conn = _FakeConn([_POS])
    listener = _FeedListener(feed)

    await listener.on_position_updated("0", _POS)
    await listener.on_position_removed("0", "96484066")

    updates = await _drain(feed)
    assert [u.resync for u in updates] == [False, False]


async def test_snapshot_reads_terminal_state():
    feed = MetaApiFeed("token", "acct")
    feed._conn = _FakeConn([_POS])
    snap = await feed.snapshot()
    assert [p.position_id for p in snap] == ["96484066"]
