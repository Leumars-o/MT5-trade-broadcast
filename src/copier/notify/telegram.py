"""TelegramNotifier — send-only Telegram Bot API sink over httpx.

Retries with exponential backoff on rate limits (429, honouring
``retry_after``), 5xx, and transport errors. Other 4xx responses are permanent
and are not retried. The bot token is never logged, never printed, and never
included in an exception message — it lives only in the request URL.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
from pydantic import SecretStr

from ..logging_config import get_logger

log = get_logger("copier.notify.telegram")

_API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(
        self,
        bot_token: SecretStr,
        chat_id: str,
        *,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 4,
        base_delay: float = 0.5,
        timeout: float = 10.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    @property
    def _url(self) -> str:
        # Token is confined to the URL; it is never logged.
        return f"{_API_BASE}/bot{self._token.get_secret_value()}/sendMessage"

    async def send(self, text: str) -> bool:
        payload = {"chat_id": self._chat_id, "text": text}
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = await self._client.post(self._url, json=payload)
            except httpx.HTTPError as exc:
                # Transport-level failure — retry with backoff.
                if not await self._backoff(attempt, reason=type(exc).__name__):
                    log.warning("telegram_send_failed", reason="transport", attempts=attempt)
                    return False
                continue

            if resp.status_code == 200 and resp.json().get("ok") is True:
                log.info("telegram_sent", attempts=attempt, chars=len(text))
                return True

            if resp.status_code == 429:
                retry_after = self._retry_after(resp)
                if not await self._backoff(attempt, reason="rate_limited", delay=retry_after):
                    log.warning("telegram_send_failed", reason="rate_limited", attempts=attempt)
                    return False
                continue

            if 500 <= resp.status_code < 600:
                if not await self._backoff(attempt, reason=f"http_{resp.status_code}"):
                    log.warning("telegram_send_failed", status=resp.status_code, attempts=attempt)
                    return False
                continue

            # Permanent client error (bad chat_id, bad token, malformed) — do not
            # retry. Never log the response body: it can echo the token.
            log.warning("telegram_send_rejected", status=resp.status_code, attempts=attempt)
            return False

        return False

    async def _backoff(self, attempt: int, *, reason: str, delay: float | None = None) -> bool:
        """Sleep before the next attempt. Returns False if attempts are exhausted."""
        if attempt >= self._max_attempts:
            return False
        wait = delay if delay is not None else self._base_delay * (2 ** (attempt - 1))
        log.info("telegram_retry", reason=reason, attempt=attempt, wait_s=round(wait, 2))
        await self._sleep(wait)
        return True

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        try:
            return float(resp.json().get("parameters", {}).get("retry_after"))
        except (ValueError, TypeError, AttributeError):
            return None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> TelegramNotifier:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
