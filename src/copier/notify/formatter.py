"""Message templates (ARCHITECTURE.md §8).

**Pure functions only** — no network, no clock reads. ``now`` is always passed
in so message formatting is fully unit-testable and golden-file-pinned. Every
message carries a **signal age**; if it routinely exceeds ~5s the pipeline has a
problem you would otherwise never see.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ..core.governor import GovernorAssessment
from ..models import (
    Anomaly,
    AnomalyKind,
    CloseEstimate,
    Direction,
    GovernorVerdict,
    Position,
    SizingDecision,
    SizingRefused,
)

_DIR_EMOJI = {"buy": "🟢", "sell": "🔴"}
_VERDICT_EMOJI = {
    GovernorVerdict.OK: "✅",
    GovernorVerdict.WARN: "⚠️",
    GovernorVerdict.SKIP: "⛔",
}


# ---------------------------------------------------------------- helpers


def _row(label: str, value: str, width: int = 9) -> str:
    """A label/value row with values aligned to a common column."""
    return f"{label + ':':<{width}}{value}"


# Wider column for the close message, whose labels ("Master exit") are longer.
_CLOSE_WIDTH = 13


def fmt_age(now: datetime, reference: datetime) -> str:
    """Signal age (pipeline latency) as ``1.4s`` / ``3m 05s``."""
    secs = max(0.0, (now - reference).total_seconds())
    if secs < 60:
        return f"{secs:.1f}s"
    m, s = divmod(int(round(secs)), 60)
    return f"{m}m {s:02d}s"


def fmt_held(open_time: datetime, close_time: datetime) -> str:
    """Hold duration as ``2h 14m`` / ``14m`` / ``45s``."""
    secs = max(0, int((close_time - open_time).total_seconds()))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def fmt_duration(seconds: float) -> str:
    """A span of ``seconds`` as ``2h 14m`` / ``6m 05s`` / ``42s``."""
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def _money(value: Decimal, signed: bool = False) -> str:
    v = round(float(value))
    if signed:
        sign = "+" if v >= 0 else "-"
        return f"{sign}${abs(v):,}"
    return f"${v:,}"


def _g(value: float) -> str:
    """Compact number like ``0.001798`` / ``4399.36`` without trailing zeros."""
    return f"{value:g}"


def _sizing_note(sizing: SizingDecision) -> str:
    """Surface how much this trade leverages the strategy's native size, and
    which limit bound it — so the leverage is never invisible."""
    native_1x = (
        sizing.native_lots / sizing.size_multiplier
        if sizing.size_multiplier
        else sizing.native_lots
    )
    multiple = sizing.destination_lots / native_1x if native_1x else 0.0
    label = "risk-capped" if sizing.binding_constraint == "risk_cap" else "proportional"
    return f"{multiple:.0f}× native · {label}"


# ---------------------------------------------------------------- messages


def format_entry(
    position: Position,
    sizing: SizingDecision,
    assessment: GovernorAssessment,
    destination_symbol: str,
    now: datetime,
) -> str:
    """The entry alert. Always includes a protective stop price."""
    direction: Direction = position.direction
    emoji = _DIR_EMOJI[direction]
    pct = round(sizing.utilisation_pct * 100)

    lines = [
        f"{emoji} {direction.upper()} {destination_symbol} — {sizing.destination_lots:.2f} lots",
        _row("Master", f"{_g(position.volume)} @ {_g(position.open_price)}"),
        _row(
            "Stop",
            f"{_g(sizing.protective_stop_price)}  ({sizing.stop_distance_points:g} pts)",
        ),
        _row("Risk", f"{_money(sizing.risk_usd)}  ({pct}% of daily)"),
        _row("Budget", f"{_money(assessment.budget_remaining_usd)} remaining today"),
        _row("Sizing", _sizing_note(sizing)),
        _row("Age", f"{fmt_age(now, position.open_time)}   ·   #{position.position_id}"),
    ]
    if assessment.verdict is not GovernorVerdict.OK:
        v = assessment.verdict
        lines.append(
            f"{_VERDICT_EMOJI[v]} Governor: {v.value.upper()} — "
            f"{assessment.utilisation_pct:.0%} of daily budget committed"
        )
    return "\n".join(lines)


def format_pre_existing(
    position: Position, destination_symbol: str, now: datetime
) -> str:
    """The quieter alert for a position already running before startup: it is
    reported for awareness, not as an entry to act on (ARCHITECTURE.md §5.2)."""
    emoji = _DIR_EMOJI[position.direction]
    return "\n".join(
        [
            f"⚪ PRE-EXISTING {emoji} {position.direction.upper()} {destination_symbol}"
            f"  ·  #{position.position_id}",
            _row("Master", f"{_g(position.volume)} @ {_g(position.open_price)}"),
            _row("Opened", position.open_time.isoformat()),
            "Already open before startup — not a new entry signal.",
        ]
    )


def format_close(
    position: Position, estimate: CloseEstimate, destination_symbol: str, now: datetime
) -> str:
    """The close alert with a destination-side net estimate."""
    if position.close_price is None or position.close_time is None:
        raise ValueError("format_close requires a closed position")
    w = _CLOSE_WIDTH
    fees = estimate.commission_usd + estimate.fee_drag_usd
    lines = [
        f"✅ CLOSE {position.direction.upper()} {destination_symbol}  ·  #{position.position_id}",
        _row("Master exit", f"{_g(position.close_price)}   ({estimate.points:+.2f} pts)", w),
        _row("Gross", f"{_money(estimate.gross_usd, signed=True)}", w),
        _row("Fees", f"-{_money(fees)} (comm + PF)", w),
        _row("Your est", f"{_money(estimate.net_usd, signed=True)} net", w),
        _row("Held", fmt_held(position.open_time, position.close_time), w),
        _row("Age", fmt_age(now, position.close_time), w),
    ]
    return "\n".join(lines)


def format_refusal(
    position: Position, refusal: SizingRefused, destination_symbol: str, now: datetime
) -> str:
    """INFO alert when the engine declined to size (too small / no spec). A stop
    price is shown when one could still be computed."""
    emoji = _DIR_EMOJI[position.direction]
    header = (
        f"ℹ️ SKIP {emoji} {position.direction.upper()} {destination_symbol}"
        f"  ·  #{position.position_id}"
    )
    lines = [
        header,
        _row("Master", f"{_g(position.volume)} @ {_g(position.open_price)}"),
        _row("Reason", refusal.reason),
    ]
    if refusal.protective_stop_price is not None:
        lines.append(
            _row(
                "Stop",
                f"{_g(refusal.protective_stop_price)} "
                f"({refusal.stop_basis_points * refusal.buffer_multiplier:g} pts)",
            )
        )
    lines.append(_row("Age", f"{fmt_age(now, position.open_time)}   ·   #{position.position_id}"))
    return "\n".join(lines)


# ---------------------------------------------------------------- health / anomaly


def format_anomaly(anomaly: Anomaly) -> str:
    """Render a CRITICAL tripwire alert (§8), distinct from trade signals."""
    tag = f"  ·  #{anomaly.position_id}"
    if anomaly.kind is AnomalyKind.VOLUME_STEP:
        ratio = anomaly.ratio or 0.0
        seq = ""
        if anomaly.consecutive and anomaly.consecutive >= 2:
            seq = f"{anomaly.consecutive} consecutive steps. "
        return (
            f"⚠️ VOLUME STEP {ratio:.1f}× {anomaly.symbol}{tag}\n"
            f"Previous: {_g(anomaly.previous_volume or 0)} → now "
            f"{_g(anomaly.current_volume or 0)}\n"
            f"{seq}Review before acting."
        )
    if anomaly.kind is AnomalyKind.DIRECTION_PNL:
        return (
            f"⚠️ DIRECTION/P&L MISMATCH {anomaly.symbol}{tag}\n"
            "Recorded direction disagrees with the sign of realised profit — a "
            "mirrored trade would run backwards. Do not act."
        )
    if anomaly.kind is AnomalyKind.CONTRACT_DRIFT:
        return (
            f"⚠️ CONTRACT SIZE DRIFT {anomaly.symbol}{tag}\n"
            f"Implied {_g(anomaly.implied_contract or 0)} vs spec "
            f"{_g(anomaly.expected_contract or 0)} "
            f"({(anomaly.drift_pct or 0):.1%}). Verify oz/lot before trusting $ figures."
        )
    # OVER_HOLD
    return (
        f"⚠️ OVER-HOLD {anomaly.symbol}{tag}\n"
        f"Open {fmt_duration((anomaly.held_minutes or 0) * 60)} — past the "
        f"{anomaly.limit_minutes:g}m expected max. Master may be stuck."
    )


def format_health_stale(now: datetime, last_snapshot: datetime, threshold_minutes: float) -> str:
    """CRITICAL: the feed has gone quiet — the dead-man's switch (§5.6)."""
    gap = (now - last_snapshot).total_seconds()
    return (
        f"🛑 FEED STALE — no snapshot for {fmt_duration(gap)} "
        f"(threshold {threshold_minutes:g}m)\n"
        f"Last snapshot at {last_snapshot.isoformat()}. Process may be wedged."
    )


def format_health_disconnect(now: datetime) -> str:
    return f"🔌 DISCONNECTED from broker feed at {now.isoformat()}."


def format_health_reconnect(downtime_seconds: float, now: datetime) -> str:
    return f"🔗 RECONNECTED to broker feed · downtime {fmt_duration(downtime_seconds)}."


def format_daily_summary(
    local_date: str,
    signal_count: int,
    max_signal_age_ms: int,
    provenance: str,
    now: datetime,
    last_snapshot: datetime | None,
) -> str:
    """The daily 'still alive' summary. Surfaces signal throughput, the worst
    observed signal age, and the risk-parameter provenance so the stop basis and
    the unmeasured MAE cannot be quietly forgotten (§10.6)."""
    feed = (
        f"{fmt_duration((now - last_snapshot).total_seconds())} ago"
        if last_snapshot is not None
        else "never"
    )
    return "\n".join(
        [
            f"🩺 copier-bot alive · {local_date}",
            _row("Signals", str(signal_count)),
            _row("Max age", f"{max_signal_age_ms / 1000:.1f}s"),
            _row("Feed", f"last snapshot {feed}"),
            _row("Risk", provenance),
        ]
    )
