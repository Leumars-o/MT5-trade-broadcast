"""`log-fill` persistence: recording manual executions against alerts (§6)."""

from __future__ import annotations

import pytest

from copier.models import EventType
from copier.store.repo import Repo

from .conftest import at


def _repo(tmp_path) -> Repo:
    repo = Repo(tmp_path / "copier.db")
    repo.initialize()
    return repo


def test_log_execution_against_an_alert(tmp_path):
    repo = _repo(tmp_path)
    alert_id = repo.insert_alert("96484066", EventType.OPENED, {"text": "x"}, detected_at=at(0))
    assert alert_id is not None

    repo.log_execution(
        alert_id, acted=True, fill_price=4399.5, fill_time=at(1),
        actual_lots=0.03, notes="filled a hair late",
    )
    rows = repo.list_executions()
    assert len(rows) == 1
    assert rows[0]["alert_id"] == alert_id
    assert rows[0]["acted"] == 1
    assert rows[0]["fill_price"] == 4399.5
    assert rows[0]["position_id"] == "96484066"  # joined from alerts


def test_log_execution_not_acted(tmp_path):
    repo = _repo(tmp_path)
    alert_id = repo.insert_alert("1", EventType.OPENED, {}, detected_at=at(0))
    assert alert_id is not None
    repo.log_execution(alert_id, acted=False, notes="too small to bother")
    row = repo.list_executions()[0]
    assert row["acted"] == 0
    assert row["fill_price"] is None


def test_execution_rejects_unknown_alert_id(tmp_path):
    import sqlite3

    repo = _repo(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        repo.log_execution(999, acted=True, fill_price=1.0)
