"""DailyGovernor — daily budget accounting (ARCHITECTURE.md §5.4).

Tracks, per **broker server day** (not local day), the risk committed by open
signals. Produces an OK / WARN / SKIP verdict against soft/hard thresholds of
the daily drawdown limit. v1 never blocks anything — it advises; the verdict and
remaining budget go into the alert.

Pure and deterministic: the server day is passed in, never read from a clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..config import GovernorConfig, RiskConfig
from ..models import GovernorVerdict


@dataclass(frozen=True, slots=True)
class GovernorAssessment:
    """The governor's view after accounting for a prospective trade."""

    verdict: GovernorVerdict
    committed_usd: Decimal        # total committed this day, incl. this trade
    budget_remaining_usd: Decimal  # daily_dd_limit - committed
    daily_limit_usd: Decimal
    utilisation_pct: float         # committed / daily_dd_limit


class DailyGovernor:
    """Accumulates committed risk per server day and assesses new trades."""

    def __init__(self, risk_cfg: RiskConfig, gov_cfg: GovernorConfig) -> None:
        self._risk = risk_cfg
        self._gov = gov_cfg
        self._committed: dict[date, Decimal] = {}

    @property
    def limit(self) -> Decimal:
        return Decimal(str(self._risk.daily_dd_limit))

    def committed_on(self, server_day: date) -> Decimal:
        return self._committed.get(server_day, Decimal("0"))

    def assess(self, risk_usd: Decimal, server_day: date) -> GovernorAssessment:
        """Return the verdict for adding ``risk_usd`` on ``server_day`` **without**
        committing it. Use ``commit`` to actually book the risk."""
        committed = self.committed_on(server_day) + risk_usd
        return self._build(committed)

    def commit(self, risk_usd: Decimal, server_day: date) -> GovernorAssessment:
        """Book ``risk_usd`` against ``server_day`` and return the resulting
        assessment. Called once per accepted entry signal."""
        committed = self.committed_on(server_day) + risk_usd
        self._committed[server_day] = committed
        return self._build(committed)

    def _build(self, committed: Decimal) -> GovernorAssessment:
        limit = self.limit
        utilisation = float(committed / limit) if limit else 0.0
        if utilisation > self._gov.hard_threshold_pct:
            verdict = GovernorVerdict.SKIP
        elif utilisation > self._gov.soft_threshold_pct:
            verdict = GovernorVerdict.WARN
        else:
            verdict = GovernorVerdict.OK
        return GovernorAssessment(
            verdict=verdict,
            committed_usd=committed,
            budget_remaining_usd=limit - committed,
            daily_limit_usd=limit,
            utilisation_pct=utilisation,
        )
