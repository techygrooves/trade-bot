"""Daily-loss kill switch.

Tracks realized PnL since the start of the current UTC day and halts new entries
once the cumulative loss exceeds a configured fraction of a reference balance.
Pure and time-injectable for testing.
"""
from __future__ import annotations

from datetime import datetime, timezone


class DailyLossGuard:
    def __init__(self, daily_loss_limit_pct: float, reference_equity: float) -> None:
        self.limit_frac = daily_loss_limit_pct / 100.0
        self.reference = max(reference_equity, 1e-9)
        self._day: str | None = None
        self.realized_today = 0.0

    def _roll(self, now: datetime) -> None:
        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self.realized_today = 0.0

    def record(self, pnl: float, now: datetime | None = None) -> None:
        self._roll(now or datetime.now(timezone.utc))
        self.realized_today += pnl

    def trading_halted(self, now: datetime | None = None) -> bool:
        self._roll(now or datetime.now(timezone.utc))
        return self.realized_today <= -self.limit_frac * self.reference
