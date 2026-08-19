"""SQLite persistence layer.

Single-process, zero-ops, survives restart. The tracker uses this to recover
last-known state on boot (restart safety) and to enforce idempotency: an alert
row is inserted *before* the send, and a conflict on ``(position_id,
event_type)`` means "already handled — skip" (ARCHITECTURE.md §5.2, §10.5).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from ..models import EventType, Position
from ..timeutil import from_iso, to_iso, utcnow

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# positions.state values
STATE_OPEN = "open"
STATE_CLOSED = "closed"
STATE_PRE_EXISTING = "pre_existing"

_LIVE_STATES = (STATE_OPEN, STATE_PRE_EXISTING)


class Repo:
    """Thin wrapper over a sqlite3 connection with the copier's queries."""

    def __init__(self, db_path: str | Path = "copier.db") -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def initialize(self) -> None:
        """Create tables if they do not exist."""
        self._conn.executescript(_SCHEMA_PATH.read_text())
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Repo:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- positions

    def load_open_positions(self) -> list[Position]:
        """Return positions still live (open / pre_existing) for restart resync."""
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE state IN (?, ?)", _LIVE_STATES
        ).fetchall()
        return [self._row_to_position(r) for r in rows]

    def upsert_position(self, pos: Position, state: str, now: datetime | None = None) -> None:
        """Insert or update a position, preserving ``first_seen`` across updates."""
        now = now or utcnow()
        now_iso = to_iso(now)
        self._conn.execute(
            """
            INSERT INTO positions (
              position_id, symbol, direction, volume, open_price, open_time,
              sl, tp, close_price, close_time, profit, first_seen, last_seen, state
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(position_id) DO UPDATE SET
              symbol=excluded.symbol,
              direction=excluded.direction,
              volume=excluded.volume,
              open_price=excluded.open_price,
              open_time=excluded.open_time,
              sl=excluded.sl,
              tp=excluded.tp,
              close_price=excluded.close_price,
              close_time=excluded.close_time,
              profit=excluded.profit,
              last_seen=excluded.last_seen,
              state=excluded.state
            """,
            (
                pos.position_id,
                pos.symbol,
                pos.direction,
                pos.volume,
                pos.open_price,
                to_iso(pos.open_time),
                pos.sl,
                pos.tp,
                pos.close_price,
                to_iso(pos.close_time),
                pos.profit,
                now_iso,
                now_iso,
                state,
            ),
        )
        self._conn.commit()

    # ---------------------------------------------------------------- alerts

    def alert_exists(self, position_id: str, event_type: EventType) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM alerts WHERE position_id = ? AND event_type = ?",
            (position_id, event_type.value),
        ).fetchone()
        return row is not None

    def insert_alert(
        self,
        position_id: str,
        event_type: EventType,
        payload: dict[str, object],
        detected_at: datetime,
        broker_event_time: datetime | None = None,
        signal_age_ms: int | None = None,
    ) -> int | None:
        """Insert an alert row *before* sending. Returns the new id, or ``None``
        if an alert for ``(position_id, event_type)`` already exists (idempotency
        — the caller must then skip the send)."""
        try:
            cur = self._conn.execute(
                """
                INSERT INTO alerts (
                  position_id, event_type, broker_event_time, detected_at,
                  signal_age_ms, payload_json, send_status
                ) VALUES (?,?,?,?,?,?,'pending')
                """,
                (
                    position_id,
                    event_type.value,
                    to_iso(broker_event_time),
                    to_iso(detected_at),
                    signal_age_ms,
                    json.dumps(payload, default=str),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid) if cur.lastrowid is not None else None
        except sqlite3.IntegrityError:
            return None

    def mark_alert_sent(self, alert_id: int, sent_at: datetime | None = None) -> None:
        self._conn.execute(
            "UPDATE alerts SET send_status='sent', sent_at=? WHERE id=?",
            (to_iso(sent_at or utcnow()), alert_id),
        )
        self._conn.commit()

    def mark_alert_failed(self, alert_id: int) -> None:
        self._conn.execute(
            "UPDATE alerts SET send_status='failed' WHERE id=?", (alert_id,)
        )
        self._conn.commit()

    # ---------------------------------------------------------------- health

    def record_heartbeat(
        self, now: datetime | None = None, last_snapshot: datetime | None = None
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO health_heartbeat (id, last_beat, last_snapshot)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              last_beat=excluded.last_beat,
              last_snapshot=COALESCE(excluded.last_snapshot, health_heartbeat.last_snapshot)
            """,
            (to_iso(now or utcnow()), to_iso(last_snapshot)),
        )
        self._conn.commit()

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _row_to_position(r: sqlite3.Row) -> Position:
        open_time = from_iso(r["open_time"])
        assert open_time is not None  # NOT NULL in schema
        return Position(
            position_id=r["position_id"],
            symbol=r["symbol"],
            direction=r["direction"],
            volume=r["volume"],
            open_price=r["open_price"],
            open_time=open_time,
            sl=r["sl"],
            tp=r["tp"],
            close_price=r["close_price"],
            close_time=from_iso(r["close_time"]),
            profit=r["profit"],
        )
