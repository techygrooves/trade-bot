"""Event-driven backtester for the trend-momentum strategy.

Design choices that keep results honest (no lookahead, realistic fills):
  * Decisions on bar i use only data known at i's close (via compute_features).
  * Orders execute at the NEXT bar's open.
  * A stop-loss is a standing order checked intrabar: if the next bar's low
    pierces the stop, we exit at the stop price.
  * Position size comes from the risk model: risk a fixed % of equity, sized
    from the stop distance, never exceeding available equity (spot = no leverage).
  * Taker fees are charged on both entry and exit.

This is a single-symbol, one-position-at-a-time baseline. Scaled take-profits
and trailing stops (see PLAN.md) are planned refinements; the baseline exits on
a stop hit or a higher-timeframe trend flip.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.config import RiskConfig, StrategyConfig
from src.strategy.signals import compute_features

from .metrics import Metrics, compute_metrics


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity_curve: pd.Series
    metrics: Metrics


def run_backtest(
    signal_df: pd.DataFrame,
    trend_df: pd.DataFrame,
    strategy_cfg: StrategyConfig,
    risk_cfg: RiskConfig,
    initial_equity: float = 10_000.0,
    symbol: str = "ASSET",
) -> BacktestResult:
    feats = compute_features(signal_df, trend_df, strategy_cfg)

    open_ = feats["open"].to_numpy()
    high = feats["high"].to_numpy()
    low = feats["low"].to_numpy()
    close = feats["close"].to_numpy()
    atr = feats["atr"].to_numpy()
    entry_sig = feats["entry_signal"].to_numpy()
    exit_sig = feats["exit_signal"].to_numpy()
    times = feats.index
    n = len(feats)

    fee = risk_cfg.taker_fee_pct / 100.0
    risk_frac = risk_cfg.risk_per_trade_pct / 100.0

    equity = initial_equity
    in_pos = False
    entry_price = stop = qty = risk_amount = 0.0
    entry_time = None

    trades: list[dict] = []
    eq_times = [times[0]]
    eq_values = [equity]

    def close_trade(exit_price: float, exit_time, reason: str) -> None:
        nonlocal equity, in_pos
        entry_fee = qty * entry_price * fee
        exit_fee = qty * exit_price * fee
        net_pnl = qty * (exit_price - entry_price) - entry_fee - exit_fee
        equity += net_pnl
        trades.append(
            {
                "symbol": symbol,
                "entry_time": entry_time,
                "entry_price": entry_price,
                "exit_time": exit_time,
                "exit_price": exit_price,
                "qty": qty,
                "stop": stop,
                "net_pnl": net_pnl,
                "r_multiple": net_pnl / risk_amount if risk_amount > 0 else 0.0,
                "exit_reason": reason,
                "equity_after": equity,
            }
        )
        eq_times.append(exit_time)
        eq_values.append(equity)
        in_pos = False

    for i in range(n - 1):
        nxt = i + 1
        if in_pos:
            # Stop check first (worst case fills before any trend-flip exit).
            if low[nxt] <= stop:
                close_trade(stop, times[nxt], "stop")
            elif bool(exit_sig[i]):
                close_trade(open_[nxt], times[nxt], "trend_exit")
        elif bool(entry_sig[i]) and atr[i] > 0:
            entry_price = float(open_[nxt])
            stop = entry_price - strategy_cfg.atr_stop_mult * float(atr[i])
            risk_per_unit = entry_price - stop
            if risk_per_unit <= 0:
                continue
            risk_amount = equity * risk_frac
            qty = risk_amount / risk_per_unit
            # Spot: cannot spend more than we have (fees included).
            max_qty = equity / (entry_price * (1 + fee))
            if qty > max_qty:
                qty = max_qty
                risk_amount = qty * risk_per_unit
            entry_time = times[nxt]
            in_pos = True

    # Close any open position at the final bar's close.
    if in_pos:
        close_trade(float(close[-1]), times[-1], "end_of_data")

    trades_df = pd.DataFrame(trades)
    equity_curve = pd.Series(eq_values, index=pd.DatetimeIndex(eq_times), name="equity")
    metrics = compute_metrics(trades_df, equity_curve, initial_equity)
    return BacktestResult(trades=trades_df, equity_curve=equity_curve, metrics=metrics)
