"""Deterministic tests for the backtester's exit schemes and fill mechanics.

These drive `simulate()` directly with hand-built feature frames, so entries
and price paths are exact and every fill rule can be asserted precisely
(rather than hoping a random walk happens to trigger each code path).

Fixed geometry throughout: ATR = 1, atr_stop_mult = 2, so a position entered
at 100 has stop 98 (1R = 2.0), 1.5R take-profit 103, 3R take-profit 106.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import simulate
from src.backtest.metrics import exit_reason_stats
from src.config import ExitConfig, RiskConfig, StrategyConfig

STRAT = StrategyConfig()          # atr_stop_mult = 2.0
NO_FRICTION = RiskConfig(taker_fee_pct=0.0, slippage_bps=0.0)
SCALED = ExitConfig(
    scheme="scaled", tp1_r=1.5, tp1_frac=1 / 3, tp2_r=3.0, tp2_frac=1 / 3,
    trail_atr_mult=2.0,
)


def _feats(rows: list[tuple]) -> pd.DataFrame:
    """rows: (open, high, low, close, entry_signal, exit_signal); ATR = 1."""
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="1h", tz="UTC")
    o, h, l, c, e, x = zip(*rows)
    return pd.DataFrame(
        {
            "open": o, "high": h, "low": l, "close": c,
            "atr": 1.0, "entry_signal": e, "exit_signal": x,
        },
        index=idx,
    )


def _run(rows, exit_cfg, risk=NO_FRICTION):
    return simulate(_feats(rows), STRAT, risk, exit_cfg)


def test_fixed_tp_full_exit_at_target():
    res = _run(
        [
            (100, 100, 100, 100, True, False),   # signal at this close
            (100, 105, 99.5, 104, False, False),  # entry 100, TP 103 hit
            (104, 104, 104, 104, False, False),
        ],
        ExitConfig(scheme="fixed_tp", reward_mult=1.5),
    )
    assert len(res.trades) == 1
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "take_profit"
    assert t["entry_price"] == pytest.approx(100.0)
    assert t["exit_price"] == pytest.approx(103.0)
    assert t["partial_exits"] == 0
    # Risked 1% of 10k = 100 over a 2.0 stop distance -> qty 50; +3/unit = 1.5R.
    assert t["net_pnl"] == pytest.approx(150.0)
    assert t["r_multiple"] == pytest.approx(1.5)


def test_stop_fills_before_take_profit_when_bar_covers_both():
    res = _run(
        [
            (100, 100, 100, 100, True, False),
            (100, 105, 97, 100, False, False),  # range covers stop 98 AND tp 103
        ],
        ExitConfig(scheme="fixed_tp", reward_mult=1.5),
    )
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(98.0)


def test_gap_through_stop_fills_at_open_not_stop():
    res = _run(
        [
            (100, 100, 100, 100, True, False),
            (100, 101, 99, 100, False, False),  # entry bar, stop 98 untouched
            (95, 96, 94, 95, False, False),     # opens well below the stop
        ],
        ExitConfig(scheme="trend"),
    )
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(95.0)  # the open, worse than 98


def test_entry_bar_can_stop_out_same_bar():
    res = _run(
        [
            (100, 100, 100, 100, True, False),
            (100, 100.5, 97, 98, False, False),  # low pierces stop 98 immediately
        ],
        ExitConfig(scheme="trend"),
    )
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "stop"
    assert t["exit_time"] == t["entry_time"]
    assert t["exit_price"] == pytest.approx(98.0)


def test_trend_scheme_exits_at_next_open_on_flip():
    res = _run(
        [
            (100, 100, 100, 100, True, False),
            (100, 101, 99, 100, False, True),   # trend flips at this close
            (102, 102, 102, 102, False, False),  # market out at the open
        ],
        ExitConfig(scheme="trend"),
    )
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "trend_exit"
    assert t["exit_price"] == pytest.approx(102.0)


def test_scaled_partials_then_trailing_stop():
    res = _run(
        [
            (100, 100, 100, 100, True, False),
            (100, 100.5, 99.5, 100, False, False),   # entry; trail stays 98
            (100, 103.5, 100, 103, False, False),    # tp1 103: sell 1/3; trail -> 101
            (103, 106.5, 103, 106, False, False),    # tp2 106: sell 1/3; trail -> 104
            (105, 105.5, 103.5, 104, False, False),  # low 103.5 <= 104: trail out
        ],
        SCALED,
    )
    assert len(res.trades) == 1
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "trail_stop"
    assert t["partial_exits"] == 2
    # Thirds at 103, 106 and 104 -> weighted exit 104.333...
    assert t["exit_price"] == pytest.approx((103 + 106 + 104) / 3)
    # qty 50, avg +4.333/unit, initial risk 100 -> ~2.17R
    assert t["net_pnl"] == pytest.approx(50 * 13 / 3)
    assert t["r_multiple"] == pytest.approx(50 * 13 / 3 / 100)
    assert "trail_stop" in exit_reason_stats(res.trades).index


def test_scaled_trailing_stop_never_moves_down():
    # After trailing to 104 (close 106), a deep red close must NOT lower it:
    # the position still exits at 104, not at 100 - 2 = 98-style recompute.
    res = _run(
        [
            (100, 100, 100, 100, True, False),
            (100, 106.5, 100, 106, False, False),   # tp1+tp2 same bar; trail -> 104
            (105, 105, 104.5, 105, False, False),   # holds above the trail
            (105, 105, 103.9, 104, False, False),   # pierces 104 -> out at 104
        ],
        SCALED,
    )
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "trail_stop"
    assert t["partial_exits"] == 2
    # Final third filled exactly at the ratcheted stop 104.
    assert t["exit_price"] == pytest.approx((103 + 106 + 104) / 3)


def test_slippage_applies_to_market_fills_not_limits():
    slipped = RiskConfig(taker_fee_pct=0.0, slippage_bps=100)  # 1%
    res = _run(
        [
            (100, 100, 100, 100, True, False),
            (100, 110, 99.5, 108, False, False),  # entry 101; tp 101+1.5*2=104
            (108, 108, 108, 108, False, False),
        ],
        ExitConfig(scheme="fixed_tp", reward_mult=1.5),
        risk=slipped,
    )
    t = res.trades.iloc[0]
    assert t["entry_price"] == pytest.approx(101.0)     # market entry pays slip
    assert t["exit_price"] == pytest.approx(104.0)      # limit TP fills exactly
    assert t["stop"] == pytest.approx(99.0)             # stop from slipped entry


def test_open_position_marks_equity_to_market():
    res = _run(
        [
            (100, 100, 100, 100, True, False),
            (100, 100.5, 99, 99, False, False),   # unrealized loss, stop 98 safe
            (99, 99.5, 98.5, 99, False, False),   # closed as end_of_data
        ],
        ExitConfig(scheme="trend"),
    )
    assert res.trades.iloc[0]["exit_reason"] == "end_of_data"
    # The bar with the open drawdown shows it, even though no trade closed there.
    assert res.equity_curve.iloc[1] == pytest.approx(10_000 - 50 * 1.0)
    assert res.metrics.max_drawdown_pct > 0


def test_fees_charged_on_every_fill():
    fee_risk = RiskConfig(taker_fee_pct=0.1, slippage_bps=0.0)
    res = _run(
        [
            (100, 100, 100, 100, True, False),
            (100, 105, 99.5, 104, False, False),
            (104, 104, 104, 104, False, False),
        ],
        ExitConfig(scheme="fixed_tp", reward_mult=1.5),
        risk=fee_risk,
    )
    t = res.trades.iloc[0]
    qty = t["qty"]
    expected = qty * 3.0 - qty * 100 * 0.001 - qty * 103 * 0.001
    assert t["net_pnl"] == pytest.approx(expected)
    assert t["r_multiple"] < 1.5  # fees eat into the gross 1.5R


def test_metrics_report_sharpe_and_holding_time():
    res = _run(
        [
            (100, 100, 100, 100, True, False),
            (100, 105, 99.5, 104, False, False),
            (104, 104, 104, 104, False, False),
            (104, 104, 104, 104, False, False),
        ],
        ExitConfig(scheme="fixed_tp", reward_mult=1.5),
    )
    assert np.isfinite(res.metrics.sharpe)
    assert res.metrics.sharpe > 0          # only equity move is the winner
    assert res.metrics.avg_hold_hours == pytest.approx(0.0)  # same-bar exit


def test_simulate_rejects_missing_columns():
    df = _feats([(100, 100, 100, 100, False, False)] * 3).drop(columns=["atr"])
    with pytest.raises(ValueError, match="atr"):
        simulate(df, STRAT, NO_FRICTION, ExitConfig())
