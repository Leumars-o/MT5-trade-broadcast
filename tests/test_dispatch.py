"""AlertDispatcher: insert-before-send idempotency, and the key M3 guarantee —
a restart mid-run produces no duplicate sends (ARCHITECTURE.md §9 M3, §10.5)."""

from __future__ import annotations

from copier.models import EventType
from copier.notify.dispatcher import AlertDispatcher, DispatchResult
from copier.store.repo import Repo

from .conftest import at


class FakeNotifier:
    """Records every message actually sent, and can be told to fail."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    async def send(self, text: str) -> bool:
        if self.ok:
            self.sent.append(text)
        return self.ok


def _repo(tmp_path) -> Repo:
    repo = Repo(tmp_path / "copier.db")
    repo.initialize()
    return repo


async def test_first_dispatch_sends(tmp_path):
    repo = _repo(tmp_path)
    notifier = FakeNotifier()
    d = AlertDispatcher(repo, notifier)

    result = await d.dispatch(
        position_id="1", event_type=EventType.OPENED.value, message="hello",
        detected_at=at(1), broker_event_time=at(0),
    )
    assert result is DispatchResult.SENT
    assert notifier.sent == ["hello"]


async def test_duplicate_dispatch_is_skipped_not_resent(tmp_path):
    repo = _repo(tmp_path)
    notifier = FakeNotifier()
    d = AlertDispatcher(repo, notifier)

    await d.dispatch(position_id="1", event_type="opened", message="hello", detected_at=at(1))
    result = await d.dispatch(
        position_id="1", event_type="opened", message="hello again", detected_at=at(2)
    )
    assert result is DispatchResult.SKIPPED_DUPLICATE
    assert notifier.sent == ["hello"]  # the second send never happened


async def test_restart_midrun_produces_no_duplicate_sends(tmp_path):
    """Simulate a crash+restart: a fresh dispatcher over the SAME database must
    not resend an alert that was already sent before the restart."""
    db = tmp_path / "copier.db"

    repo1 = Repo(db)
    repo1.initialize()
    notifier1 = FakeNotifier()
    await AlertDispatcher(repo1, notifier1).dispatch(
        position_id="96484066", event_type="opened", message="ENTRY", detected_at=at(1)
    )
    await AlertDispatcher(repo1, notifier1).dispatch(
        position_id="96484066", event_type="closed", message="CLOSE", detected_at=at(2)
    )
    repo1.close()
    assert notifier1.sent == ["ENTRY", "CLOSE"]

    # Restart: new repo on the same file, new notifier, replay the same events.
    repo2 = Repo(db)
    repo2.initialize()
    notifier2 = FakeNotifier()
    d2 = AlertDispatcher(repo2, notifier2)
    r_open = await d2.dispatch(
        position_id="96484066", event_type="opened", message="ENTRY", detected_at=at(1)
    )
    r_close = await d2.dispatch(
        position_id="96484066", event_type="closed", message="CLOSE", detected_at=at(2)
    )
    repo2.close()

    assert r_open is DispatchResult.SKIPPED_DUPLICATE
    assert r_close is DispatchResult.SKIPPED_DUPLICATE
    assert notifier2.sent == []  # nothing resent after restart


async def test_distinct_event_types_both_send(tmp_path):
    repo = _repo(tmp_path)
    notifier = FakeNotifier()
    d = AlertDispatcher(repo, notifier)
    await d.dispatch(position_id="1", event_type="opened", message="o", detected_at=at(1))
    await d.dispatch(position_id="1", event_type="closed", message="c", detected_at=at(2))
    # Distinct MODIFY discriminators are not collapsed.
    await d.dispatch(
        position_id="1", event_type="modified:sl:None", message="m1", detected_at=at(3)
    )
    await d.dispatch(
        position_id="1", event_type="modified:volume:-0.1", message="m2", detected_at=at(4)
    )
    assert notifier.sent == ["o", "c", "m1", "m2"]


async def test_failed_send_marks_failed_and_allows_no_phantom(tmp_path):
    repo = _repo(tmp_path)
    notifier = FakeNotifier(ok=False)
    d = AlertDispatcher(repo, notifier)
    result = await d.dispatch(position_id="1", event_type="opened", message="x", detected_at=at(1))
    assert result is DispatchResult.FAILED
    assert notifier.sent == []
    row = repo._conn.execute(
        "SELECT send_status FROM alerts WHERE position_id='1'"
    ).fetchone()
    assert row["send_status"] == "failed"
