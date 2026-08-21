"""Typed configuration loaded from ``config/config.yaml`` + ``.env``.

Non-secret config lives in the YAML file; ``${VAR}`` placeholders are resolved
from the environment (populated from ``.env``) at load time. Secrets are never
stored in the YAML and never logged.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, SecretStr

_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")


class MasterConfig(BaseModel):
    metaapi_account_id: str = ""
    server_timezone: str = "Etc/GMT-3"
    poll_interval_seconds: float = 2.0
    read_only: bool = True  # investor credentials only; never disable


class SymbolConfig(BaseModel):
    destination_symbol: str
    contract_size_fallback: float


class RiskConfig(BaseModel):
    # Stop basis: worst realised loss (p100) from the 74-trade sample, NOT an MAE.
    stop_basis_points: float = 3.90
    buffer_multiplier: float = 1.5
    # Balance-proportional sizing: the master trades ~0.00077 lots per $1k of its
    # balance. size_multiplier=1.0 reproduces that native profile on the
    # destination equity; >1 deliberately leverages it (edge unproven above 1×).
    native_lots_per_1k: float = 0.00077
    size_multiplier: float = 1.0
    commission_per_lot: float = 10.0
    # Prop-firm fee drag: ~26% of gross profit on the sample (net ≈ ¾ of gross).
    fee_drag_pct: float = 0.26
    daily_dd_limit: float = 2500
    max_dd_limit: float = 5000
    utilisation_target: float = 0.15    # per-trade RISK CAP, not the size driver
    destination_equity: float = 50000
    # True floating MAE — still UNMEASURED; do not use for sizing until measured.
    mae_points: float | None = None


class GovernorConfig(BaseModel):
    soft_threshold_pct: float = 0.60
    hard_threshold_pct: float = 0.85


class TelegramConfig(BaseModel):
    bot_token: SecretStr = SecretStr("")
    chat_id: str = ""


class HealthConfig(BaseModel):
    # Wedge backstop: how long with no *healthy* feed poll before alerting while
    # still nominally connected. A live disconnect alerts immediately (separately),
    # so this only needs to catch a stalled pipeline. Kept short — many master
    # trades close in <5 min, so a slow watchdog can't protect them.
    stale_feed_minutes: float = 1.5
    daily_summary_time: str = "22:00"
    max_expected_hold_minutes: float = 240
    # External dead-man's switch (e.g. healthchecks.io). The bot pings this URL
    # every heartbeat; the external service alerts if the pings stop — the only
    # thing that catches total process/host death. Empty = disabled.
    heartbeat_ping_url: str = ""


class Settings(BaseModel):
    """The fully-resolved application configuration."""

    master: MasterConfig = Field(default_factory=MasterConfig)
    symbols: dict[str, SymbolConfig] = Field(default_factory=dict)
    risk: RiskConfig
    governor: GovernorConfig = Field(default_factory=GovernorConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    display_timezone: str = "UTC"


def _expand_env(value: Any) -> Any:
    """Recursively replace ``${VAR}`` placeholders using the environment.

    An unset variable resolves to an empty string so the app still loads for
    milestones that do not need that secret (e.g. no Telegram token in M1).
    """
    if isinstance(value, str):
        return _ENV_PLACEHOLDER.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _load_dotenv(path: Path) -> None:
    """Minimal ``.env`` loader (no external dep). Does not override existing env."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def load_settings(
    config_path: str | Path = "config/config.yaml",
    env_path: str | Path = ".env",
) -> Settings:
    """Load and validate configuration. Fails fast (pydantic) on a bad value."""
    _load_dotenv(Path(env_path))
    raw = yaml.safe_load(Path(config_path).read_text()) or {}
    return Settings.model_validate(_expand_env(raw))
