"""AlertDispatcher — idempotent persist-then-send.

The order matters (ARCHITECTURE.md §10.5): the alert row is inserted *before* the
send. A conflict on ``(position_id, event_type)`` means the alert was already
handled, so the send is skipped. This is what makes a restart mid-run produce no
duplicate Telegram messages: on replay, the insert conflicts and nothing is sent
again.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from ..store.repo import Repo
from ..timeutil import utcnow
from .base import Notifier


class DispatchResult(StrEnum):
    SENT = "sent"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    FAILED = "failed"


class AlertDispatcher:
    def __init__(self, repo: Repo, notifier: Notifier) -> None:
        self._repo = repo
        self._notifier = notifier

    async def dispatch(
        self,
        *,
        position_id: str,
        event_type: str,
        message: str,
        detected_at: datetime,
        broker_event_time: datetime | None = None,
        now: datetime | None = None,
    ) -> DispatchResult:
        """Insert the alert (dedupe), then send if newly inserted."""
        now = now or utcnow()
        signal_age_ms: int | None = None
        if broker_event_time is not None:
            signal_age_ms = max(0, int((now - broker_event_time).total_seconds() * 1000))

        alert_id = self._repo.insert_alert(
            position_id=position_id,
            event_type=event_type,
            payload={"text": message},
            detected_at=detected_at,
            broker_event_time=broker_event_time,
            signal_age_ms=signal_age_ms,
        )
        if alert_id is None:
            return DispatchResult.SKIPPED_DUPLICATE

        ok = await self._notifier.send(message)
        if ok:
            self._repo.mark_alert_sent(alert_id, sent_at=now)
            return DispatchResult.SENT
        self._repo.mark_alert_failed(alert_id)
        return DispatchResult.FAILED
