"""PositionTracker — diffs successive position snapshots into events.

This is a pure, in-memory state machine (no I/O, no clock reads — ``now`` is
passed in) so every restart and reconnect case is deterministically testable.

Design requirements (ARCHITECTURE.md §5.2):

* **Restart safety.** State recovered from the store is passed to ``prime``.
  A position seen for the first time whose ``open_time`` predates process start
  is emitted as ``PRE_EXISTING`` (a quieter alert), never ``OPENED`` — otherwise
  a restart spams fake entry signals for positions already running.
* **Reconnect safety.** The first snapshot after a websocket drop is a *resync*:
  it reconciles state silently and never emits a ``CLOSED``, so a dropped
  connection cannot produce phantom closes for every open position.
* **Idempotency.** Emitting an event is separate from acting on it; the store
  enforces uniqueness on ``(position_id, event_type)`` before any send.
"""

from __future__ import annotations

from datetime import datetime

from ..models import EventType, Position, PositionEvent

# Fields whose change constitutes a MODIFIED event. open_price/open_time/symbol
# are identity-ish and should not drift for a live position; volume/sl/tp are the
# fields a master actually mutates. A volume *decrease* is a partial close.
_TRACKED_FIELDS = ("volume", "sl", "tp")


class PositionTracker:
    """Holds the last-known snapshot and emits events on each new one."""

    def __init__(self, process_start_time: datetime) -> None:
        """``process_start_time`` (aware UTC) is the boundary that distinguishes
        a genuinely new open from a pre-existing position on a cold start."""
        self._process_start_time = process_start_time
        self._known: dict[str, Position] = {}

    def prime(self, positions: list[Position]) -> None:
        """Seed last-known state from the store on boot. Priming emits nothing:
        these positions were already observed in a previous run, so they must not
        re-fire as OPENED/PRE_EXISTING."""
        self._known = {p.position_id: p for p in positions}

    @property
    def known(self) -> dict[str, Position]:
        return dict(self._known)

    def diff(
        self, snapshot: list[Position], now: datetime, *, resync: bool = False
    ) -> list[PositionEvent]:
        """Compare ``snapshot`` against last-known state and return the events.

        When ``resync=True`` (the first snapshot after a reconnect) state is
        reconciled *without* emitting any event — in particular no ``CLOSED`` is
        produced for a position merely absent from a post-reconnect snapshot.
        """
        if resync:
            self._resync(snapshot)
            return []

        events: list[PositionEvent] = []
        snapshot_ids = {p.position_id for p in snapshot}

        # Opens, pre-existing, and modifications.
        for pos in snapshot:
            prev = self._known.get(pos.position_id)
            if prev is None:
                events.append(self._new_event(pos, now))
            else:
                modified = self._modified_event(prev, pos, now)
                if modified is not None:
                    events.append(modified)

        # Closes: known before, absent now.
        for pid, prev in self._known.items():
            if pid not in snapshot_ids:
                events.append(
                    PositionEvent(
                        event_type=EventType.CLOSED,
                        position=prev,
                        detected_at=now,
                        previous=prev,
                    )
                )

        self._known = {p.position_id: p for p in snapshot}
        return events

    # ---------------------------------------------------------------- internals

    def _resync(self, snapshot: list[Position]) -> None:
        """Merge a post-reconnect snapshot into known state without emitting.

        Merge (not replace): positions absent from this snapshot are *kept*, so a
        partial post-reconnect snapshot never silently drops — and never phantom
        closes — them. Genuine closes surface naturally on the next healthy diff.
        """
        for pos in snapshot:
            self._known[pos.position_id] = pos

    def _new_event(self, pos: Position, now: datetime) -> PositionEvent:
        """A never-before-seen id is OPENED if it opened after we started,
        else PRE_EXISTING (it was already running on a cold start)."""
        event_type = (
            EventType.OPENED
            if pos.open_time >= self._process_start_time
            else EventType.PRE_EXISTING
        )
        return PositionEvent(event_type=event_type, position=pos, detected_at=now)

    @staticmethod
    def _modified_event(
        prev: Position, curr: Position, now: datetime
    ) -> PositionEvent | None:
        changed = tuple(
            f for f in _TRACKED_FIELDS if getattr(prev, f) != getattr(curr, f)
        )
        if not changed:
            return None
        volume_delta = None
        if "volume" in changed:
            volume_delta = curr.volume - prev.volume
        return PositionEvent(
            event_type=EventType.MODIFIED,
            position=curr,
            detected_at=now,
            previous=prev,
            changed_fields=changed,
            volume_delta=volume_delta,
        )
