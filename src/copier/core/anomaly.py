"""AnomalyDetector — the tripwires that fire a distinct CRITICAL alert, separate
from trade signals (ARCHITECTURE.md §5.5):

* volume step ≥ 2.0× the previous master trade (martingale / pyramiding watch —
  this is exactly what flagged the duplicated 0.001798 signal in the sample)
* on close, ``sign(price_move × direction) != sign(profit)`` — the direction
  field disagrees with realised P&L, i.e. mirrored trades would run backwards
* implied contract size drifts >2% from the broker spec
* a position stays open beyond ``max_expected_hold_minutes``

Pure and numeric: it holds only the small state needed (previous volume, which
positions already over-hold-alerted) and takes ``now`` explicitly. The formatter
renders the messages.
"""

from __future__ import annotations

from datetime import datetime

from ..models import Anomaly, AnomalyKind, Position, SymbolSpec
from .risk import signed_points


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


class AnomalyDetector:
    def __init__(
        self,
        *,
        volume_step_factor: float = 2.0,
        drift_tolerance: float = 0.02,
        max_hold_minutes: float = 240.0,
    ) -> None:
        self._volume_step_factor = volume_step_factor
        self._drift_tolerance = drift_tolerance
        self._max_hold_minutes = max_hold_minutes
        self._last_volume: float | None = None
        self._consecutive_steps = 0
        self._over_hold_flagged: set[str] = set()

    def on_open(self, position: Position) -> Anomaly | None:
        """Check the new master trade's volume against the previous one."""
        prev = self._last_volume
        self._last_volume = position.volume
        if prev is None or prev <= 0:
            return None
        ratio = position.volume / prev
        if ratio >= self._volume_step_factor:
            self._consecutive_steps += 1
            return Anomaly(
                kind=AnomalyKind.VOLUME_STEP,
                position_id=position.position_id,
                symbol=position.symbol,
                previous_volume=prev,
                current_volume=position.volume,
                ratio=ratio,
                consecutive=self._consecutive_steps,
            )
        self._consecutive_steps = 0
        return None

    def on_close(self, position: Position, spec: SymbolSpec | None) -> list[Anomaly]:
        """Checks that only make sense once a trade has realised P&L."""
        out: list[Anomaly] = []
        points = signed_points(position)
        profit = position.profit

        # Direction field vs realised P&L sign.
        if profit is not None and profit != 0 and points != 0:
            if _sign(points) != _sign(profit):
                out.append(
                    Anomaly(
                        kind=AnomalyKind.DIRECTION_PNL,
                        position_id=position.position_id,
                        symbol=position.symbol,
                    )
                )

        # Implied contract size vs broker spec (approximate: gross profit only).
        if spec is not None and profit is not None and points != 0 and position.volume > 0:
            implied = profit / (points * position.volume)
            if spec.contract_size > 0:
                drift = abs(implied / spec.contract_size - 1)
                if drift > self._drift_tolerance:
                    out.append(
                        Anomaly(
                            kind=AnomalyKind.CONTRACT_DRIFT,
                            position_id=position.position_id,
                            symbol=position.symbol,
                            expected_contract=spec.contract_size,
                            implied_contract=implied,
                            drift_pct=drift,
                        )
                    )
        return out

    def check_holds(self, open_positions: list[Position], now: datetime) -> list[Anomaly]:
        """Flag positions open past the max expected hold — once each."""
        out: list[Anomaly] = []
        live_ids = {p.position_id for p in open_positions}
        # Let a position re-arm once it closes.
        self._over_hold_flagged &= live_ids
        for p in open_positions:
            held_min = (now - p.open_time).total_seconds() / 60.0
            if held_min > self._max_hold_minutes and p.position_id not in self._over_hold_flagged:
                self._over_hold_flagged.add(p.position_id)
                out.append(
                    Anomaly(
                        kind=AnomalyKind.OVER_HOLD,
                        position_id=p.position_id,
                        symbol=p.symbol,
                        held_minutes=held_min,
                        limit_minutes=self._max_hold_minutes,
                    )
                )
        return out
