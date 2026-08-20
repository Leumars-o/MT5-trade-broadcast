"""Guard: configure_logging must silence httpx/httpcore, whose INFO request-line
logs would otherwise print the bot token embedded in the Telegram API URL
(ARCHITECTURE.md §10.8)."""

from __future__ import annotations

import logging

from copier.logging_config import configure_logging


def test_httpx_loggers_are_quieted():
    configure_logging(level="INFO", json_output=True)
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_structlog_redacts_credential_keys(capsys):
    from copier.logging_config import get_logger

    configure_logging(level="INFO", json_output=True)
    get_logger("test").info("event", bot_token="super-secret", chat_id="123")
    out = capsys.readouterr().out
    assert "super-secret" not in out
    assert "***REDACTED***" in out
