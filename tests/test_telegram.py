"""TelegramNotifier: retry/backoff behaviour, driven by a mocked httpx transport
so no real network is touched. Sleeps are captured, not awaited for real."""

from __future__ import annotations

import httpx
from pydantic import SecretStr

from copier.notify.telegram import TelegramNotifier


class _Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _notifier(handler, sleeps: _Sleeps, **kw) -> TelegramNotifier:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramNotifier(
        SecretStr("TOKEN"), "chat-1", client=client, base_delay=0.5, sleep=sleeps, **kw
    )


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


async def test_send_success_no_retry():
    sleeps = _Sleeps()
    n = _notifier(_ok, sleeps)
    assert await n.send("hi") is True
    assert sleeps.calls == []
    await n.aclose()


async def test_token_never_in_logs_but_used_in_url():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _ok(request)

    n = _notifier(handler, _Sleeps())
    await n.send("hi")
    assert "botTOKEN" in seen["url"]  # token lives only in the URL
    await n.aclose()


async def test_retries_on_429_then_succeeds():
    calls = {"n": 0}
    sleeps = _Sleeps()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 3}})
        return _ok(request)

    n = _notifier(handler, sleeps)
    assert await n.send("hi") is True
    assert calls["n"] == 2
    assert sleeps.calls == [3.0]  # honoured retry_after, not the default backoff
    await n.aclose()


async def test_retries_on_5xx_with_exponential_backoff():
    calls = {"n": 0}
    sleeps = _Sleeps()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"ok": False})
        return _ok(request)

    n = _notifier(handler, sleeps)
    assert await n.send("hi") is True
    assert calls["n"] == 3
    assert sleeps.calls == [0.5, 1.0]  # 0.5 * 2^0, 0.5 * 2^1
    await n.aclose()


async def test_gives_up_after_max_attempts():
    calls = {"n": 0}
    sleeps = _Sleeps()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"ok": False})

    n = _notifier(handler, sleeps, max_attempts=3)
    assert await n.send("hi") is False
    assert calls["n"] == 3  # tried 3 times
    assert len(sleeps.calls) == 2  # slept between attempts, not after the last
    await n.aclose()


async def test_permanent_4xx_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"ok": False, "description": "bad chat"})

    n = _notifier(handler, _Sleeps())
    assert await n.send("hi") is False
    assert calls["n"] == 1  # no retry on a permanent client error
    await n.aclose()


async def test_transport_error_is_retried():
    calls = {"n": 0}
    sleeps = _Sleeps()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return _ok(request)

    n = _notifier(handler, sleeps)
    assert await n.send("hi") is True
    assert calls["n"] == 2
    assert sleeps.calls == [0.5]
    await n.aclose()
