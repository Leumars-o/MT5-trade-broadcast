"""ReplayFeed parses the xlsx fixture and reconstructs the snapshot timeline,
and drives the tracker end-to-end with no network."""

from __future__ import annotations

from pathlib import Path

import pytest

from copier.core.tracker import PositionTracker
from copier.feed.replay_feed import ReplayFeed, build_snapshots, parse_positions
from copier.models import EventType

from .fixtures._make_report import write_report

FIXTURE = Path(__file__).parent / "fixtures" / "ReportHistory-51104542.xlsx"


def test_parse_positions_reads_all_trades():
    positions = parse_positions(FIXTURE)
    ids = {p.position_id for p in positions}
    assert ids == {"96484060", "96484061", "96484066", "96484070"}

    # The buy with no close_time is still open.
    still_open = next(p for p in positions if p.position_id == "96484070")
    assert still_open.direction == "buy"
    assert still_open.close_time is None


def test_open_times_are_utc_aware():
    for p in parse_positions(FIXTURE):
        assert p.open_time.tzinfo is not None
        assert p.open_time.utcoffset().total_seconds() == 0


def test_build_snapshots_tracks_open_set_over_time():
    positions = parse_positions(FIXTURE)
    snaps = build_snapshots(positions)
    # Boundaries: opens 09:00,09:15,10:05,11:00 + closes 09:30,10:00,12:19 = 7.
    assert len(snaps) == 7

    # Position count open at each boundary is never negative and the final
    # boundary (a close) leaves the still-open buy running.
    last_time, last_open = snaps[-1]
    open_ids = {p.position_id for p in last_open}
    assert "96484070" in open_ids  # the never-closed buy


async def test_replay_drives_tracker_end_to_end():
    feed = ReplayFeed(FIXTURE)
    await feed.connect()
    process_start = feed.snapshots[0][0]
    tracker = PositionTracker(process_start_time=process_start)

    counts: dict[EventType, int] = {}
    async for update in feed.stream():
        for ev in tracker.diff(update.positions, now=update.server_time):
            counts[ev.event_type] = counts.get(ev.event_type, 0) + 1

    # Every trade opens once; three of the four close within the report window.
    assert counts.get(EventType.OPENED, 0) == 4
    assert counts.get(EventType.CLOSED, 0) == 3
    assert counts.get(EventType.PRE_EXISTING, 0) == 0


async def test_custom_report_round_trips(tmp_path):
    path = write_report(
        tmp_path / "r.xlsx",
        rows=[
            ("2025.08.18 08:00:00", 111, "XAUUSD.f", "buy", 0.05, 4400.0,
             0, 0, "2025.08.18 08:30:00", 4405.0, -0.5, 0.0, 25.0),
        ],
    )
    positions = parse_positions(path)
    assert len(positions) == 1
    assert positions[0].position_id == "111"
    assert positions[0].direction == "buy"


async def test_symbol_spec_marks_fallback():
    feed = ReplayFeed(FIXTURE)
    spec = await feed.symbol_spec("XAUUSD.f")
    assert spec.contract_size == pytest.approx(100.0)
    assert spec.from_fallback is True
