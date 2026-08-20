"""M2 entry point: ReplayFeed → tracker → risk → governor → formatter → stdout.

No network, no Telegram yet (M3 adds the sink). This wires the risk engine and
formatter behind the tracker so the full alert text can be observed end-to-end
over the xlsx fixture. The pipeline is deliberately synchronous and side-effect
free apart from printing.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import Settings, load_settings
from .core.governor import DailyGovernor
from .core.risk import estimate_close, size_position
from .core.tracker import PositionTracker
from .feed.replay_feed import ReplayFeed
from .logging_config import configure_logging, get_logger
from .models import EventType, Position, PositionEvent, SizingDecision
from .notify import formatter
from .timeutil import utcnow

log = get_logger("copier.main")


def _destination_symbol(settings: Settings, symbol: str) -> str:
    """Map a master symbol to its destination symbol, else strip the suffix."""
    mapping = settings.symbols.get(symbol)
    if mapping is not None:
        return mapping.destination_symbol
    return symbol.split(".")[0].upper()


class ReplayPipeline:
    """Turns tracker events into formatted alert text (the M3 sink slots in here)."""

    def __init__(self, feed: ReplayFeed, settings: Settings) -> None:
        self._feed = feed
        self._settings = settings
        self._governor = DailyGovernor(settings.risk, settings.governor)
        # Remember the lots we sized at entry so the close estimate is consistent.
        self._sized_lots: dict[str, float] = {}

    async def handle(self, event: PositionEvent) -> str | None:
        pos = event.position
        dest = _destination_symbol(self._settings, pos.symbol)

        if event.event_type in (EventType.OPENED, EventType.PRE_EXISTING):
            return await self._on_open(event, pos, dest)
        if event.event_type is EventType.CLOSED:
            return await self._on_close(pos, dest)
        if event.event_type is EventType.MODIFIED:
            return _describe_modified(event)
        return None

    async def _on_open(self, event: PositionEvent, pos: Position, dest: str) -> str:
        now = pos.open_time
        if event.event_type is EventType.PRE_EXISTING:
            return formatter.format_pre_existing(pos, dest, now)

        spec = await self._feed.symbol_spec(pos.symbol)
        result = size_position(pos, spec, self._settings.risk)
        # Auditability (§5.3 / §10.6): record which mae value produced this size.
        log.info(
            "sized",
            position_id=pos.position_id,
            mae_points=result.mae_points_used,
            buffer=result.buffer_multiplier,
        )
        if isinstance(result, SizingDecision):
            self._sized_lots[pos.position_id] = result.destination_lots
            assessment = self._governor.commit(result.risk_usd, pos.open_time.date())
            return formatter.format_entry(pos, result, assessment, dest, now)
        return formatter.format_refusal(pos, result, dest, now)

    async def _on_close(self, pos: Position, dest: str) -> str | None:
        lots = self._sized_lots.pop(pos.position_id, None)
        if lots is None:
            # A close for a position we never sized (e.g. pre-existing) — report
            # the master exit without a destination estimate.
            return f"✅ CLOSE {pos.direction.upper()} {dest}  ·  #{pos.position_id} (not sized)"
        if pos.close_time is None:
            return None
        spec = await self._feed.symbol_spec(pos.symbol)
        estimate = estimate_close(pos, spec, lots, self._settings.risk)
        return formatter.format_close(pos, estimate, dest, pos.close_time)


def _describe_modified(event: PositionEvent) -> str:
    p = event.position
    detail = ", ".join(event.changed_fields)
    if event.volume_delta is not None:
        kind = "partial close" if event.volume_delta < 0 else "add"
        detail += f" ({kind} {event.volume_delta:+g})"
    return f"✏️ MODIFY {p.direction.upper()} {p.symbol}  ·  #{p.position_id}  [{detail}]"


async def run_replay(report: Path, settings: Settings, server_tz: str | None) -> int:
    """Replay a report through the full M2 pipeline, printing each alert."""
    feed = ReplayFeed(report, server_tz=server_tz)
    await feed.connect()

    process_start = feed.snapshots[0][0] if feed.snapshots else utcnow()
    tracker = PositionTracker(process_start_time=process_start)
    pipeline = ReplayPipeline(feed, settings)

    total = 0
    async for update in feed.stream():
        for ev in tracker.diff(update.positions, now=update.server_time or utcnow()):
            message = await pipeline.handle(ev)
            if message:
                print(message)
                print()
                total += 1
    print(f"{total} alert(s) from {len(feed.snapshots)} snapshot(s).")
    return total


def cli() -> None:
    ap = argparse.ArgumentParser(description="copier-bot M2 replay runner")
    ap.add_argument(
        "report",
        nargs="?",
        default="tests/fixtures/ReportHistory-51104542.xlsx",
        help="MT5 .xlsx history report to replay",
    )
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument(
        "--server-tz",
        default=None,
        help="Broker server timezone for naive report timestamps (e.g. Etc/GMT-3)",
    )
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    configure_logging(level=args.log_level, json_output=False)
    settings = load_settings(args.config)
    asyncio.run(run_replay(Path(args.report), settings, args.server_tz))


if __name__ == "__main__":
    cli()
