"""Config loading: YAML + ${ENV} expansion, secrets kept out of plain fields."""

from __future__ import annotations

from copier.config import Settings, load_settings


def test_loads_repo_config(monkeypatch):
    monkeypatch.setenv("METAAPI_ACCOUNT_ID", "acct-123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-9")

    settings = load_settings("config/config.yaml")

    assert isinstance(settings, Settings)
    assert settings.master.metaapi_account_id == "acct-123"
    assert settings.risk.stop_basis_points == 3.90
    assert settings.risk.buffer_multiplier == 1.5
    assert settings.risk.sizing_mode == "ceiling"
    assert settings.risk.ceiling_points == 5.0
    assert settings.risk.mae_points == 10.39  # M5 excursion estimate, not a sizing input
    assert settings.risk.utilisation_target == 0.15
    assert "XAUUSD.f" in settings.symbols
    assert settings.symbols["XAUUSD.f"].destination_symbol == "XAUUSD"


def test_secret_not_exposed_in_repr(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret")
    settings = load_settings("config/config.yaml")

    # SecretStr keeps the token out of __repr__/str.
    assert "super-secret" not in repr(settings.telegram)
    assert settings.telegram.bot_token.get_secret_value() == "super-secret"


def test_unset_env_placeholder_is_empty(monkeypatch):
    monkeypatch.delenv("METAAPI_ACCOUNT_ID", raising=False)
    # Point at a non-existent .env so the developer's real .env is not loaded.
    settings = load_settings("config/config.yaml", env_path="/nonexistent/.env")
    # Missing secret resolves to empty string, not the literal ${VAR}.
    assert settings.master.metaapi_account_id == ""
