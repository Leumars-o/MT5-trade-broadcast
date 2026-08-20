"""Entry point: feed → tracker → risk → governor → formatter → sink.

Three run modes:
  * default   — replay the xlsx fixture, print alerts to stdout (no network)
  * --send    — replay + dispatch to Telegram idempotently
  * --live    — connect the MetaApi read-only feed and dispatch (shadow mode)
  * --send-test — send one Telegram message and exit

The pipeline is identical across modes; only the feed and the sink differ.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .core.governor import DailyGovernor
from .core.risk import estimate_close, size_position
from .core.tracker import PositionTracker
from .feed.base import FeedUpdate
from .feed.replay_feed import ReplayFeed
from .health import HealthMonitor
from .logging_config import configure_logging, get_logger
from .models import EventType, Position, PositionEvent, SizingDecision
from .notify import formatter
from .notify.dispatcher import AlertDispatcher
from .notify.telegram import TelegramNotifier
from .store.repo import STATE_CLOSED, STATE_OPEN, STATE_PRE_EXISTING, Repo
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


def _broker_time(event: PositionEvent) -> Any:
    if event.event_type is EventType.CLOSED and event.position.close_time is not None:
        return event.position.close_time
    return event.position.open_time


_STATE_FOR_EVENT = {
    EventType.OPENED: STATE_OPEN,
    EventType.MODIFIED: STATE_OPEN,
    EventType.PRE_EXISTING: STATE_PRE_EXISTING,
    EventType.CLOSED: STATE_CLOSED,
}


class Pipeline:
    """Turns tracker events into formatted alert text (feed-agnostic)."""

    def __init__(self, feed: Any, settings: Settings) -> None:
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
            stop_basis_points=result.stop_basis_points,
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
            # Live feed knows the position closed but not its exit price/time
            # (that lives in the deal stream — a later enrichment). Report plainly.
            return f"✅ CLOSE {pos.direction.upper()} {dest}  ·  #{pos.position_id}"
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


async def consume_feed(
    feed: Any,
    settings: Settings,
    tracker: PositionTracker,
    *,
    dispatcher: AlertDispatcher | None = None,
    repo: Repo | None = None,
    monitor: HealthMonitor | None = None,
) -> int:
    """Drive one feed through the pipeline. Honours resync, persists position
    state for restart recovery, prints, and optionally dispatches. When a
    ``monitor`` is supplied, each snapshot beats the heartbeat and each alert is
    counted for the daily summary."""
    pipeline = Pipeline(feed, settings)
    total = 0
    async for update in feed.stream():
        assert isinstance(update, FeedUpdate)
        now = update.server_time or utcnow()
        if monitor is not None:
            monitor.record_snapshot(now)
        events = tracker.diff(update.positions, now=now, resync=update.resync)
        for ev in events:
            if repo is not None:
                repo.upsert_position(ev.position, _STATE_FOR_EVENT[ev.event_type], now=now)
            message = await pipeline.handle(ev)
            if not message:
                continue
            print(message)
            total += 1
            if monitor is not None:
                broker_time = _broker_time(ev)
                monitor.record_signal(int(max(0.0, (now - broker_time).total_seconds() * 1000)))
            if dispatcher is not None:
                result = await dispatcher.dispatch(
                    position_id=ev.position_id,
                    event_type=_event_key(ev),
                    message=message,
                    detected_at=now,
                    broker_event_time=_broker_time(ev),
                    now=now,
                )
                print(f"   → {result.value}")
            print()
    return total


# ---------------------------------------------------------------- run modes


async def run_replay(
    report: Path, settings: Settings, server_tz: str | None,
    dispatcher: AlertDispatcher | None = None, repo: Repo | None = None,
) -> int:
    feed = ReplayFeed(report, server_tz=server_tz)
    await feed.connect()
    process_start = feed.snapshots[0][0] if feed.snapshots else utcnow()
    tracker = PositionTracker(process_start_time=process_start)
    if repo is not None:
        tracker.prime(repo.load_open_positions())
    total = await consume_feed(feed, settings, tracker, dispatcher=dispatcher, repo=repo)
    print(f"{total} alert(s).")
    return total


def _provenance(settings: Settings) -> str:
    """One-line risk-parameter provenance for the daily summary (§10.6)."""
    return (
        f"{settings.risk.stop_basis_points:g}pt stop basis (p100 realised loss) "
        "· MAE unmeasured"
    )


async def run_live(settings: Settings, token: str, db: str) -> None:
    # Imported here so the SDK is only required on the live path.
    from .feed.metaapi_feed import MetaApiFeed

    repo = Repo(db)
    repo.initialize()
    tracker = PositionTracker(process_start_time=utcnow())
    tracker.prime(repo.load_open_positions())  # restart safety

    notifier = _build_notifier(settings)
    log.warning("live_shadow_mode", note="read-only observer; alerts only, no orders")
    async with notifier:
        dispatcher = AlertDispatcher(repo, notifier)
        monitor = HealthMonitor(
            repo, dispatcher, settings.health,
            display_tz=settings.display_timezone, provenance=_provenance(settings),
        )
        feed = MetaApiFeed(
            token, settings.master.metaapi_account_id,
            read_only=settings.master.read_only,
            on_connection_change=monitor.note_connection,
        )
        await feed.connect()
        stop = asyncio.Event()
        health_task = asyncio.create_task(monitor.run(stop))
        try:
            await consume_feed(
                feed, settings, tracker, dispatcher=dispatcher, repo=repo, monitor=monitor
            )
        finally:
            stop.set()
            await health_task
            await feed.close()
    repo.close()


def _build_notifier(settings: Settings) -> TelegramNotifier:
    token = settings.telegram.bot_token.get_secret_value()
    chat_id = settings.telegram.chat_id
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env to send.")
    return TelegramNotifier(settings.telegram.bot_token, chat_id)


async def send_test(settings: Settings) -> None:
    notifier = _build_notifier(settings)
    async with notifier:
        ok = await notifier.send("✅ copier-bot: Telegram sink is live (test message).")
    print("sent" if ok else "FAILED — check token/chat_id (nothing is logged)")


async def _run_send(report: Path, settings: Settings, server_tz: str | None, db: str) -> None:
    repo = Repo(db)
    repo.initialize()
    notifier = _build_notifier(settings)
    async with notifier:
        await run_replay(report, settings, server_tz, AlertDispatcher(repo, notifier), repo)
    repo.close()


def cli() -> None:
    ap = argparse.ArgumentParser(description="copier-bot runner")
    ap.add_argument("report", nargs="?", default="tests/fixtures/ReportHistory-51104542.xlsx")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--server-tz", default=None)
    ap.add_argument("--db", default="copier.db", help="SQLite path for dedupe/state")
    ap.add_argument("--send", action="store_true", help="replay + dispatch to Telegram")
    ap.add_argument("--live", action="store_true", help="connect the MetaApi read-only live feed")
    ap.add_argument("--send-test", action="store_true", help="send one test message and exit")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    configure_logging(level=args.log_level, json_output=False)
    settings = load_settings(args.config)

    if args.send_test:
        asyncio.run(send_test(settings))
    elif args.live:
        import os

        token = os.environ.get("METAAPI_TOKEN", "")
        if not token or not settings.master.metaapi_account_id:
            raise SystemExit("METAAPI_TOKEN and METAAPI_ACCOUNT_ID must be set in .env for --live.")
        asyncio.run(run_live(settings, token, args.db))
    elif args.send:
        asyncio.run(_run_send(Path(args.report), settings, args.server_tz, args.db))
    else:
        asyncio.run(run_replay(Path(args.report), settings, args.server_tz))


if __name__ == "__main__":
    cli()
