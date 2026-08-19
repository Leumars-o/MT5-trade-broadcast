"""M1 entry point: run ReplayFeed → PositionTracker → stdout.

No network, no Telegram, no risk sizing. This wires the pieces together so the
tracker's event stream can be observed end-to-end over the xlsx fixture. Later
milestones insert AnomalyDetector / RiskEngine / Governor / Formatter / Telegram
between the tracker and the sink.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .core.tracker import PositionTracker
from .feed.replay_feed import ReplayFeed
from .logging_config import configure_logging, get_logger
from .models import EventType, PositionEvent
from .timeutil import utcnow

log = get_logger("copier.main")

# Emoji per event, purely for the readable stdout of M1.
_ICON = {
    EventType.OPENED: "🟢",
    EventType.PRE_EXISTING: "⚪",
    EventType.MODIFIED: "✏️",
    EventType.CLOSED: "✅",
}


def _describe(event: PositionEvent) -> str:
    p = event.position
    icon = _ICON.get(event.event_type, "•")
    base = (
        f"{icon} {event.event_type.value.upper():12} {p.direction.upper():4} "
        f"{p.symbol:10} {p.volume:g} @ {p.open_price:g}  #{p.position_id}"
    )
    if event.event_type is EventType.MODIFIED:
        detail = ", ".join(event.changed_fields)
        if event.volume_delta is not None:
            kind = "partial close" if event.volume_delta < 0 else "add"
            detail += f" ({kind} {event.volume_delta:+g})"
        base += f"  [{detail}]"
    return base


async def run_replay(report: Path, server_tz: str | None) -> int:
    """Replay a report through the tracker, printing each event. Returns count."""
    feed = ReplayFeed(report, server_tz=server_tz)
    await feed.connect()

    # Cold start: treat the earliest boundary as process start so pre-report
    # positions would read as PRE_EXISTING. With a full history export every
    # position opens within the window, so these read as OPENED — which is
    # correct for a replay.
    process_start = feed.snapshots[0][0] if feed.snapshots else utcnow()
    tracker = PositionTracker(process_start_time=process_start)

    total = 0
    async for update in feed.stream():
        events = tracker.diff(update.positions, now=update.server_time or utcnow())
        for ev in events:
            print(_describe(ev))
            total += 1
    print(f"\n{total} event(s) from {len(feed.snapshots)} snapshot(s).")
    return total


def cli() -> None:
    ap = argparse.ArgumentParser(description="copier-bot M1 replay runner")
    ap.add_argument(
        "report",
        nargs="?",
        default="tests/fixtures/ReportHistory-51104542.xlsx",
        help="MT5 .xlsx history report to replay",
    )
    ap.add_argument(
        "--server-tz",
        default=None,
        help="Broker server timezone for naive report timestamps (e.g. Etc/GMT-3)",
    )
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    configure_logging(level=args.log_level, json_output=False)
    asyncio.run(run_replay(Path(args.report), args.server_tz))


if __name__ == "__main__":
    cli()
