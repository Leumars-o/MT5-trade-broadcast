"""PositionTracker behaviour — the M1 restart / reconnect / dedupe / partial-close
requirements from ARCHITECTURE.md §5.2, plus the ordinary open/modify/close path.
"""

from __future__ import annotations

import pytest

from copier.core.tracker import PositionTracker
from copier.models import EventType

from .conftest import at, make_position


def _types(events):
    return [e.event_type for e in events]


# ---------------------------------------------------------------- ordinary path


def test_new_position_after_start_is_opened():
    tracker = PositionTracker(process_start_time=at(0))
    pos = make_position("1", open_time=at(5))  # opened after we started

    events = tracker.diff([pos], now=at(5))

    assert _types(events) == [EventType.OPENED]
    assert events[0].position_id == "1"


def test_disappeared_position_is_closed():
    tracker = PositionTracker(process_start_time=at(0))
    pos = make_position("1", open_time=at(1))
    tracker.diff([pos], now=at(1))

    events = tracker.diff([], now=at(10))

    assert _types(events) == [EventType.CLOSED]
    assert events[0].position_id == "1"


def test_sl_tp_change_emits_modified_without_volume_delta():
    tracker = PositionTracker(process_start_time=at(0))
    pos = make_position("1", open_time=at(1), sl=None, tp=None)
    tracker.diff([pos], now=at(1))

    moved = pos.with_updates(sl=4410.0, tp=4380.0)
    events = tracker.diff([moved], now=at(2))

    assert _types(events) == [EventType.MODIFIED]
    assert set(events[0].changed_fields) == {"sl", "tp"}
    assert events[0].volume_delta is None


# ---------------------------------------------------------------- restart safety


def test_preexisting_not_opened_on_cold_start():
    """A position already open before we started must be PRE_EXISTING, not
    OPENED — otherwise a restart spams fake entry signals."""
    tracker = PositionTracker(process_start_time=at(60))
    already_open = make_position("1", open_time=at(0))  # predates process start

    events = tracker.diff([already_open], now=at(60))

    assert _types(events) == [EventType.PRE_EXISTING]


def test_primed_state_from_store_emits_nothing():
    """Positions recovered from SQLite on boot must not re-fire as events."""
    tracker = PositionTracker(process_start_time=at(60))
    prior = make_position("1", open_time=at(0))
    tracker.prime([prior])

    events = tracker.diff([prior], now=at(61))

    assert events == []


def test_mixed_cold_start_preexisting_and_new():
    tracker = PositionTracker(process_start_time=at(30))
    old = make_position("old", open_time=at(0))
    fresh = make_position("new", open_time=at(31))

    events = tracker.diff([old, fresh], now=at(31))

    by_id = {e.position_id: e.event_type for e in events}
    assert by_id == {"old": EventType.PRE_EXISTING, "new": EventType.OPENED}


# ---------------------------------------------------------------- reconnect safety


def test_reconnect_resync_emits_no_phantom_closed():
    """A dropped websocket must not produce a CLOSED for every open position:
    the first snapshot after reconnect is a resync, not a diff source."""
    tracker = PositionTracker(process_start_time=at(0))
    a = make_position("a", open_time=at(1))
    b = make_position("b", open_time=at(1))
    tracker.diff([a, b], now=at(1))  # both known and open

    # Reconnect delivers an empty/partial first snapshot.
    events = tracker.diff([], now=at(5), resync=True)

    assert events == []
    # State preserved — a genuine later close still fires exactly once.
    assert set(tracker.known) == {"a", "b"}


def test_close_after_resync_fires_on_next_healthy_snapshot():
    tracker = PositionTracker(process_start_time=at(0))
    a = make_position("a", open_time=at(1))
    b = make_position("b", open_time=at(1))
    tracker.diff([a, b], now=at(1))
    tracker.diff([], now=at(5), resync=True)  # phantom-close suppressed

    # b really closed; next healthy snapshot has only a.
    events = tracker.diff([a], now=at(6))

    assert _types(events) == [EventType.CLOSED]
    assert events[0].position_id == "b"


def test_resync_adds_new_position_without_emitting():
    tracker = PositionTracker(process_start_time=at(0))
    a = make_position("a", open_time=at(1))
    tracker.diff([a], now=at(1))

    b = make_position("b", open_time=at(2))
    events = tracker.diff([a, b], now=at(3), resync=True)

    assert events == []
    assert set(tracker.known) == {"a", "b"}


# ---------------------------------------------------------------- idempotency


def test_duplicate_snapshot_emits_no_duplicate_events():
    tracker = PositionTracker(process_start_time=at(0))
    pos = make_position("1", open_time=at(1))
    first = tracker.diff([pos], now=at(1))
    assert _types(first) == [EventType.OPENED]

    # Identical snapshot again — nothing changed.
    second = tracker.diff([pos], now=at(2))
    assert second == []


# ---------------------------------------------------------------- partial close


def test_partial_close_is_modified_with_negative_volume_delta():
    """A volume decrease is a partial close: MODIFIED, not CLOSED."""
    tracker = PositionTracker(process_start_time=at(0))
    pos = make_position("1", volume=0.40, open_time=at(1))
    tracker.diff([pos], now=at(1))

    reduced = pos.with_updates(volume=0.25)
    events = tracker.diff([reduced], now=at(2))

    assert _types(events) == [EventType.MODIFIED]
    ev = events[0]
    assert ev.changed_fields == ("volume",)
    assert ev.volume_delta == pytest.approx(-0.15)
    assert ev.is_partial_close
    assert ev.event_type is not EventType.CLOSED


def test_volume_increase_is_modified_add_not_partial_close():
    tracker = PositionTracker(process_start_time=at(0))
    pos = make_position("1", volume=0.10, open_time=at(1))
    tracker.diff([pos], now=at(1))

    added = pos.with_updates(volume=0.20)
    events = tracker.diff([added], now=at(2))

    assert events[0].event_type is EventType.MODIFIED
    assert events[0].volume_delta == pytest.approx(0.10)
    assert not events[0].is_partial_close
