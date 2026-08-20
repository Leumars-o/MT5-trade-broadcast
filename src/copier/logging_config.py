"""structlog setup — JSON to stdout, with credential redaction at the logger
level (not the call site), per ARCHITECTURE.md §10.8.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any, cast

import structlog

# Keys whose values must never appear in logs.
_REDACT_KEYS = {
    "bot_token",
    "token",
    "metaapi_token",
    "chat_id",
    "password",
    "investor_password",
    "api_key",
    "secret",
}
_REDACTED = "***REDACTED***"


def _redact(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in _REDACT_KEYS:
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper()))

    # Silence noisy third-party loggers:
    # - httpx/httpcore log the full request line at INFO, which for the Telegram
    #   API includes the bot token in the URL (ARCHITECTURE.md §10.8).
    # - socketio/engineio (used by the MetaApi SDK) log EVERY websocket packet at
    #   INFO — including the full ~200-symbol specification dump on each sync and
    #   PING/PONG every 30s. On a 24/7 service this floods journald.
    # The `.client` children are set explicitly: python-socketio only forces a
    # level when the logger is still NOTSET, so pre-setting them makes our level
    # stick even after the SDK enables its logger.
    for noisy in (
        "httpx", "httpcore",
        "socketio", "socketio.client",
        "engineio", "engineio.client",
        "metaapi_cloud_sdk",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "copier") -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
