"""Repo: restart-state recovery and idempotent (insert-before-send) alerts."""

from __future__ import annotations

from copier.models import EventType
from copier.store.repo import STATE_CLOSED, STATE_OPEN, STATE_PRE_EXISTING, Repo

from .conftest import at, make_position


def _repo(tmp_path) -> Repo:
    repo = Repo(tmp_path / "test.db")
    repo.initialize()
    return repo


def test_load_open_positions_recovers_live_state(tmp_path):
    repo = _repo(tmp_path)
    live = make_position("1", open_time=at(0))
    gone = make_position("2", open_time=at(0))
    repo.upsert_position(live, STATE_OPEN, now=at(1))
    repo.upsert_position(gone, STATE_CLOSED, now=at(1))

    recovered = repo.load_open_positions()

    assert [p.position_id for p in recovered] == ["1"]
    assert recovered[0].open_time.tzinfo is not None


def test_pre_existing_state_is_recovered_as_live(tmp_path):
    repo = _repo(tmp_path)
    repo.upsert_position(make_position("p", open_time=at(0)), STATE_PRE_EXISTING, now=at(1))
    assert [p.position_id for p in repo.load_open_positions()] == ["p"]


def test_upsert_preserves_first_seen(tmp_path):
    repo = _repo(tmp_path)
    pos = make_position("1", open_time=at(0), volume=0.10)
    repo.upsert_position(pos, STATE_OPEN, now=at(1))
    repo.upsert_position(pos.with_updates(volume=0.20), STATE_OPEN, now=at(5))

    row = repo._conn.execute(
        "SELECT first_seen, last_seen, volume FROM positions WHERE position_id='1'"
    ).fetchone()
    assert row["first_seen"] != row["last_seen"]
    assert row["volume"] == 0.20


def test_insert_alert_is_idempotent(tmp_path):
    repo = _repo(tmp_path)
    first = repo.insert_alert("1", EventType.OPENED, {"x": 1}, detected_at=at(1))
    assert first is not None
    assert repo.alert_exists("1", EventType.OPENED)

    # Same (position_id, event_type) again → conflict → None → caller skips send.
    dup = repo.insert_alert("1", EventType.OPENED, {"x": 2}, detected_at=at(2))
    assert dup is None


def test_different_event_types_are_distinct_alerts(tmp_path):
    repo = _repo(tmp_path)
    assert repo.insert_alert("1", EventType.OPENED, {}, detected_at=at(1)) is not None
    assert repo.insert_alert("1", EventType.CLOSED, {}, detected_at=at(2)) is not None


def test_mark_alert_sent(tmp_path):
    repo = _repo(tmp_path)
    aid = repo.insert_alert("1", EventType.OPENED, {}, detected_at=at(1))
    assert aid is not None
    repo.mark_alert_sent(aid, sent_at=at(2))
    row = repo._conn.execute(
        "SELECT send_status, sent_at FROM alerts WHERE id=?", (aid,)
    ).fetchone()
    assert row["send_status"] == "sent"
    assert row["sent_at"] is not None
