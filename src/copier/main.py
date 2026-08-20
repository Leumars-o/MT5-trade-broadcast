"""M3 entry point: ReplayFeed → tracker → risk → governor → formatter → sink.

Default run prints alerts to stdout (no network). With ``--send`` the alerts are
dispatched to Telegram idempotently (insert-before-send dedupe via SQLite), so a
restart mid-run produces no duplicate messages. ``--send-test`` sends a single
confirmation message and exits.
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
from .notify.dispatcher import AlertDispatcher
from .notify.telegram import TelegramNotifier
from .store.repo import Repo
from .timeutil import utcnow

log = get_logger("copier.main")


def _destination_symbol(settings: Settings, symbol: str) -> str:
    mapping = settings.symbols.get(symbol)
    if mapping is not None:
        return mapping.destination_symbol
    return symbol.split(".")[0].upper()


def _event_key(event: PositionEvent) -> str:
    """Dedupe key for the alerts table. MODIFY carries a discriminator so
    successive modifications of one position are not collapsed into one alert."""
    if event.event_type is EventType.MODIFIED:
        fields = ",".join(event.changed_fields)
        return f"modified:{fields}:{event.volume_delta}"
    return event.event_type.value


def _broker_time(event: PositionEvent) -> object:
    if event.event_type is EventType.CLOSED and event.position.close_time is not None:
        return event.position.close_time
    return event.position.open_time


class ReplayPipeline:
    """Turns tracker events into formatted alert text."""

    def __init__(self, feed: ReplayFeed, settings: Settings) -> None:
        self._feed = feed
        self._settings = settings
        self._governor = DailyGovernor(settings.risk, settings.governor)
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


async def run_replay(
    report: Path,
    settings: Settings,
    server_tz: str | None,
    dispatcher: AlertDispatcher | None = None,
) -> int:
    """Replay a report through the pipeline, printing each alert. When a
    dispatcher is supplied, alerts are also sent idempotently."""
    feed = ReplayFeed(report, server_tz=server_tz)
    await feed.connect()
    process_start = feed.snapshots[0][0] if feed.snapshots else utcnow()
    tracker = PositionTracker(process_start_time=process_start)
    pipeline = ReplayPipeline(feed, settings)

    total = 0
    async for update in feed.stream():
        now = update.server_time or utcnow()
        for ev in tracker.diff(update.positions, now=now):
            message = await pipeline.handle(ev)
            if not message:
                continue
            print(message)
            total += 1
            if dispatcher is not None:
                result = await dispatcher.dispatch(
                    position_id=ev.position_id,
                    event_type=_event_key(ev),
                    message=message,
                    detected_at=now,
                    broker_event_time=_broker_time(ev),  # type: ignore[arg-type]
                    now=now,
                )
                print(f"   → {result.value}")
            print()
    print(f"{total} alert(s) from {len(feed.snapshots)} snapshot(s).")
    return total


def _build_notifier(settings: Settings) -> TelegramNotifier:
    token = settings.telegram.bot_token.get_secret_value()
    chat_id = settings.telegram.chat_id
    if not token or not chat_id:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env to send."
        )
    return TelegramNotifier(settings.telegram.bot_token, chat_id)


async def send_test(settings: Settings) -> None:
    """Send a single confirmation message to verify the live Telegram wiring."""
    notifier = _build_notifier(settings)
    async with notifier:
        ok = await notifier.send("✅ copier-bot: M3 Telegram sink is live (test message).")
    print("sent" if ok else "FAILED — check token/chat_id (nothing is logged)")


async def _run_send(report: Path, settings: Settings, server_tz: str | None, db: str) -> None:
    repo = Repo(db)
    repo.initialize()
    notifier = _build_notifier(settings)
    async with notifier:
        dispatcher = AlertDispatcher(repo, notifier)
        await run_replay(report, settings, server_tz, dispatcher)
    repo.close()


def cli() -> None:
    ap = argparse.ArgumentParser(description="copier-bot M3 replay runner")
    ap.add_argument(
        "report",
        nargs="?",
        default="tests/fixtures/ReportHistory-51104542.xlsx",
        help="MT5 .xlsx history report to replay",
    )
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--server-tz", default=None)
    ap.add_argument("--db", default="copier.db", help="SQLite path for dedupe/state")
    ap.add_argument("--send", action="store_true", help="dispatch alerts to Telegram")
    ap.add_argument("--send-test", action="store_true", help="send one test message and exit")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    configure_logging(level=args.log_level, json_output=False)
    settings = load_settings(args.config)

    if args.send_test:
        asyncio.run(send_test(settings))
    elif args.send:
        asyncio.run(_run_send(Path(args.report), settings, args.server_tz, args.db))
    else:
        asyncio.run(run_replay(Path(args.report), settings, args.server_tz))


if __name__ == "__main__":
    cli()
