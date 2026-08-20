"""HealthMonitor — heartbeat, dead-man's switch, and the daily 'still alive'
summary (ARCHITECTURE.md §5.6). This is the component that stops a dead process
from being invisible; it runs on a task parallel to the signal pipeline.

The decision logic lives in ``tick`` / ``note_connection`` / ``record_*``, which
take ``now`` explicitly and are fully deterministic. ``run`` is the thin async
wrapper that pumps ``tick`` on an interval. Health alerts go through the same
``AlertDispatcher`` as trade alerts, so they are persisted and idempotent (a
given stall / reconnect / daily summary is sent once).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

from .config import HealthConfig
from .logging_config import get_logger
from .notify import formatter
from .notify.dispatcher import AlertDispatcher
from .store.repo import Repo
from .timeutil import to_display, utcnow

log = get_logger("copier.health")

_HEALTH_ID = "__health__"


class HealthMonitor:
    def __init__(
        self,
        repo: Repo,
        dispatcher: AlertDispatcher,
        cfg: HealthConfig,
        *,
        display_tz: str,
        provenance: str,
        heartbeat_interval: float = 30.0,
        heartbeat_ping: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._repo = repo
        self._dispatcher = dispatcher
        self._cfg = cfg
        self._display_tz = display_tz
        self._provenance = provenance
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_ping = heartbeat_ping

        self._last_snapshot: datetime | None = None
        self._connected = True
        self._disconnect_at: datetime | None = None
        self._stale_alerted = False
        self._signals = 0
        self._max_age_ms = 0
        self._summary_sent_date: object | None = None

    # ------------------------------------------------------------ event inputs

    def record_snapshot(self, now: datetime) -> None:
        """A fresh broker snapshot arrived — clears staleness, beats the heart."""
        self._last_snapshot = now
        self._stale_alerted = False
        self._repo.record_heartbeat(now=now, last_snapshot=now)

    def record_signal(self, age_ms: int | None) -> None:
        self._signals += 1
        if age_ms:
            self._max_age_ms = max(self._max_age_ms, age_ms)

    async def note_connection(self, connected: bool, now: datetime) -> None:
        """Feed connection state changed. Alerts only on an actual transition."""
        if connected and not self._connected:
            self._connected = True
            downtime = (
                (now - self._disconnect_at).total_seconds() if self._disconnect_at else 0.0
            )
            await self._send(
                f"reconnect:{now.isoformat()}",
                formatter.format_health_reconnect(downtime, now),
                now,
            )
            self._disconnect_at = None
        elif not connected and self._connected:
            self._connected = False
            self._disconnect_at = now
            await self._send(
                f"disconnect:{now.isoformat()}",
                formatter.format_health_disconnect(now),
                now,
            )

    # ------------------------------------------------------------ periodic tick

    async def tick(self, now: datetime) -> None:
        """One monitoring beat: persist the heartbeat, ping the external switch,
        check staleness, and send the daily summary when due."""
        self._repo.record_heartbeat(now=now, last_snapshot=self._last_snapshot)

        # External dead-man's switch. A failed ping must never break monitoring
        # (a transient network blip is not a reason to stop watching the feed).
        if self._heartbeat_ping is not None:
            try:
                await self._heartbeat_ping()
            except Exception as exc:  # noqa: BLE001 - deliberately swallow all
                log.warning("heartbeat_ping_failed", error=type(exc).__name__)

        if self._connected and self._last_snapshot is not None:
            gap = (now - self._last_snapshot).total_seconds()
            if gap > self._cfg.stale_feed_minutes * 60 and not self._stale_alerted:
                self._stale_alerted = True
                msg = formatter.format_health_stale(
                    now, self._last_snapshot, self._cfg.stale_feed_minutes
                )
                await self._send(f"stale:{self._last_snapshot.isoformat()}", msg, now)

        if self._summary_due(now):
            local_date = to_display(now, self._display_tz).date()
            self._summary_sent_date = local_date
            msg = formatter.format_daily_summary(
                str(local_date), self._signals, self._max_age_ms,
                self._provenance, now, self._last_snapshot,
            )
            await self._send(f"summary:{local_date}", msg, now)
            self._signals = 0
            self._max_age_ms = 0

    def _summary_due(self, now: datetime) -> bool:
        local = to_display(now, self._display_tz)
        if local.date() == self._summary_sent_date:
            return False
        hh, mm = (int(x) for x in self._cfg.daily_summary_time.split(":"))
        return (local.hour, local.minute) >= (hh, mm)

    async def _send(self, key: str, message: str, now: datetime) -> None:
        await self._dispatcher.dispatch(
            position_id=_HEALTH_ID,
            event_type=f"health:{key}",
            message=message,
            detected_at=now,
            now=now,
        )

    # ------------------------------------------------------------ run loop

    async def run(
        self,
        stop: asyncio.Event,
        *,
        now_fn: Callable[[], datetime] = utcnow,
    ) -> None:
        """Pump ``tick`` every ``heartbeat_interval`` until ``stop`` is set.

        Sleeps via ``wait_for(stop.wait())`` so a stop is honoured immediately
        rather than after a full interval.
        """
        while not stop.is_set():
            await self.tick(now_fn())
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._heartbeat_interval)
            except TimeoutError:
                pass
