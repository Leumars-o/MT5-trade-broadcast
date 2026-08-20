"""HealthMonitor: heartbeat, stale-feed dead-man's switch, disconnect/reconnect
alerts, and the daily summary. Driven with an injected clock and a real
dispatcher over a fake notifier, so idempotency is exercised too."""

from __future__ import annotations

from copier.config import HealthConfig
from copier.health import HealthMonitor
from copier.notify.dispatcher import AlertDispatcher
from copier.store.repo import Repo

from .conftest import at
from .test_dispatch import FakeNotifier

CFG = HealthConfig(stale_feed_minutes=5, daily_summary_time="22:00")
PROV = "3.9pt stop basis (p100 loss) · MAE unmeasured"


def _monitor(tmp_path):
    repo = Repo(tmp_path / "copier.db")
    repo.initialize()
    notifier = FakeNotifier()
    monitor = HealthMonitor(
        repo, AlertDispatcher(repo, notifier), CFG,
        display_tz="UTC", provenance=PROV,
    )
    return monitor, notifier


async def test_heartbeat_recorded_on_snapshot(tmp_path):
    monitor, _ = _monitor(tmp_path)
    monitor.record_snapshot(at(0))
    row = monitor._repo._conn.execute(
        "SELECT last_snapshot FROM health_heartbeat WHERE id=1"
    ).fetchone()
    assert row["last_snapshot"] is not None


async def test_no_stale_alert_within_threshold(tmp_path):
    monitor, notifier = _monitor(tmp_path)
    monitor.record_snapshot(at(0))
    await monitor.tick(at(4))  # 4 min < 5 min threshold
    assert notifier.sent == []


async def test_stale_alert_fires_once(tmp_path):
    monitor, notifier = _monitor(tmp_path)
    monitor.record_snapshot(at(0))
    await monitor.tick(at(6))   # 6 min > 5 → stale
    await monitor.tick(at(7))   # still stale, but must not re-alert
    assert len(notifier.sent) == 1
    assert "FEED STALE" in notifier.sent[0]


async def test_fresh_snapshot_clears_staleness(tmp_path):
    monitor, notifier = _monitor(tmp_path)
    monitor.record_snapshot(at(0))
    await monitor.tick(at(6))          # stale alert #1
    monitor.record_snapshot(at(7))     # recovery
    await monitor.tick(at(8))          # healthy again
    await monitor.tick(at(13))         # 6 min since at(7) → stale again
    assert [("FEED STALE" in m) for m in notifier.sent] == [True, True]
    assert len(notifier.sent) == 2


async def test_disconnect_then_reconnect_alerts_with_downtime(tmp_path):
    monitor, notifier = _monitor(tmp_path)
    await monitor.note_connection(False, at(0))
    await monitor.note_connection(False, at(1))  # no transition → no second alert
    await monitor.note_connection(True, at(3))   # downtime 3 min
    assert len(notifier.sent) == 2
    assert "DISCONNECTED" in notifier.sent[0]
    assert "RECONNECTED" in notifier.sent[1]
    assert "3m 00s" in notifier.sent[1]


async def test_daily_summary_fires_once_after_time(tmp_path):
    monitor, notifier = _monitor(tmp_path)
    monitor.record_signal(1400)
    monitor.record_signal(900)
    monitor.record_snapshot(at(59))  # keep the feed fresh (no stale alert)
    await monitor.tick(at(60))    # 10:00 — before 22:00
    assert notifier.sent == []
    monitor.record_snapshot(at(779))
    await monitor.tick(at(780))   # 22:00 — summary due
    await monitor.tick(at(781))   # same day — must not repeat
    assert len(notifier.sent) == 1
    summary = notifier.sent[0]
    assert "copier-bot alive" in summary
    assert "Signals: 2" in summary
    assert "1.4s" in summary          # max signal age
    assert "MAE unmeasured" in summary  # provenance surfaced


async def test_summary_resets_counters(tmp_path):
    monitor, notifier = _monitor(tmp_path)
    monitor.record_signal(500)
    await monitor.tick(at(780))       # summary day 1
    assert monitor._signals == 0
    assert monitor._max_age_ms == 0


async def test_heartbeat_ping_called_each_tick(tmp_path):
    repo = Repo(tmp_path / "copier.db")
    repo.initialize()
    pings = []

    async def ping() -> None:
        pings.append(1)

    monitor = HealthMonitor(
        repo, AlertDispatcher(repo, FakeNotifier()), CFG,
        display_tz="UTC", provenance=PROV, heartbeat_ping=ping,
    )
    monitor.record_snapshot(at(0))
    await monitor.tick(at(1))
    await monitor.tick(at(2))
    assert pings == [1, 1]


async def test_failed_heartbeat_ping_does_not_break_monitoring(tmp_path):
    repo = Repo(tmp_path / "copier.db")
    repo.initialize()
    notifier = FakeNotifier()

    async def ping() -> None:
        raise RuntimeError("network down")

    monitor = HealthMonitor(
        repo, AlertDispatcher(repo, notifier), CFG,
        display_tz="UTC", provenance=PROV, heartbeat_ping=ping,
    )
    monitor.record_snapshot(at(0))
    await monitor.tick(at(6))  # ping raises, but the stale check must still fire
    assert len(notifier.sent) == 1
    assert "FEED STALE" in notifier.sent[0]
