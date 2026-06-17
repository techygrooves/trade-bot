"""Tests for position sizing and the daily-loss guard."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.config import LiveConfig, RiskConfig
from src.risk.guard import DailyLossGuard
from src.risk.sizing import (
    plan_entry,
    round_step_down,
    round_tick,
    sellable_qty,
    take_profit_price,
)


def test_round_step_down():
    assert round_step_down(1.23456, 0.001) == 1.234
    assert round_step_down(0.00005, 0.0001) == 0.0
    assert round_step_down(5.0, 0.0) == 5.0  # no step -> unchanged


def test_round_tick():
    assert round_tick(100.127, 0.01) == 100.12
    assert round_tick(100.0, 0.0) == 100.0


def test_take_profit_price():
    # entry 100, stop 96 -> distance 4; reward 1.5 -> tp 106
    assert take_profit_price(100, 96, 1.5) == 106.0


def test_sellable_qty_respects_min_lot():
    assert sellable_qty(0.00123, 0.0001, 0.0001) == pytest.approx(0.0012)
    assert sellable_qty(0.00005, 0.0001, 0.0001) == 0.0


def test_plan_entry_fixed_budget():
    live = LiveConfig(sizing_mode="fixed_budget", trade_budget_usdt=10.0)
    plan = plan_entry(50.0, 100.0, 96.0, live, RiskConfig(), min_notional=5.0)
    assert plan.ok and plan.quote_to_spend == 10.0


def test_plan_entry_capped_by_balance_and_min_notional():
    live = LiveConfig(sizing_mode="fixed_budget", trade_budget_usdt=10.0)
    # Only 3 USDT free, below the 5 USDT exchange minimum -> rejected.
    plan = plan_entry(3.0, 100.0, 96.0, live, RiskConfig(), min_notional=5.0)
    assert not plan.ok and "below exchange minimum" in plan.reason


def test_plan_entry_risk_pct_mode():
    live = LiveConfig(sizing_mode="risk_pct")
    risk = RiskConfig(risk_per_trade_pct=1.0)
    # risk 1% of 1000 = 10; stop frac = 4/100 = 0.04; spend = 10/0.04 = 250
    plan = plan_entry(1000.0, 100.0, 96.0, live, risk, min_notional=5.0)
    assert plan.ok and abs(plan.quote_to_spend - 250.0) < 1e-6


def test_plan_entry_rejects_bad_stop():
    live = LiveConfig()
    plan = plan_entry(100.0, 100.0, 101.0, live, RiskConfig(), 5.0)
    assert not plan.ok


def test_daily_loss_guard_trips_and_resets():
    guard = DailyLossGuard(daily_loss_limit_pct=5.0, reference_equity=100.0)
    day1 = datetime(2024, 1, 1, 10, tzinfo=timezone.utc)
    assert not guard.trading_halted(day1)
    guard.record(-6.0, day1)  # 6 > 5% of 100
    assert guard.trading_halted(day1)
    # New UTC day resets the tally.
    day2 = datetime(2024, 1, 2, 1, tzinfo=timezone.utc)
    assert not guard.trading_halted(day2)
