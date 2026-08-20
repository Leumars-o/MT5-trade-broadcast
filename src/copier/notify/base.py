"""Notifier protocol — the seam for the notification sink. Implemented by
``TelegramNotifier`` (live) and by fakes in tests. Send-only for v1.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    async def send(self, text: str) -> bool:
        """Deliver ``text``. Return True on success, False if it could not be
        delivered after retries. Must never raise on ordinary send failure and
        must never log or embed credentials."""
        ...
