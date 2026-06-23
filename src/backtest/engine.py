"""Event-driven backtester for the trend-momentum strategy.

Design choices that keep results honest (no lookahead, realistic fills):
  * Decisions on bar i use only data known at i's close (via compute_features).
  * Orders execute at the NEXT bar's open.
  * Trade management (stop, scaled take-profits, breakeven, ATR trailing stop) is
    delegated to the SHARED bracket engine in src.strategy.exit_policy, so the
    backtest exits a position exactly the way the live trader does.
  * A stop / take-profit is a standing order checked intrabar against the next
    bar's low / high; a higher-timeframe trend flip exits at the next bar's open.
  * Position size comes from the risk model: risk a fixed % of equity, sized
    from the stop distance, never exceeding available equity (spot = no leverage).
  * Taker fees are charged on both entry and (each) exit fill.

This is a single-symbol, one-position-at-a-time baseline. A position may be
closed in several partial fills (scaled take-profits); each closed position is
recorded as one aggregated trade whose exit_reason is that of its final fill.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.config import ExitConfig, RiskConfig, StrategyConfig
from src.strategy.exit_policy import (
    REASON_END,
    REASON_TREND,
    BracketState,
    close_remaining,
    evaluate_bar,
)
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
    exit_cfg: ExitConfig | None = None,
) -> BacktestResult:
    exit_cfg = exit_cfg or ExitConfig()
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
    state: BracketState | None = None
    entry_price = qty = risk_amount = 0.0
    entry_time = None
    # Per-position accumulators (a position may close over several fills).
    pos_net_pnl = 0.0
    last_exit_price = last_exit_time = last_reason = None

    trades: list[dict] = []
    eq_times = [times[0]]
    eq_values = [equity]

    def apply_fill(fraction: float, exit_price: float, exit_time, reason: str) -> None:
        nonlocal equity, pos_net_pnl, last_exit_price, last_exit_time, last_reason
        fill_qty = fraction * qty
        entry_fee = fill_qty * entry_price * fee
        exit_fee = fill_qty * exit_price * fee
        fill_net = fill_qty * (exit_price - entry_price) - entry_fee - exit_fee
        equity += fill_net
        pos_net_pnl += fill_net
        last_exit_price, last_exit_time, last_reason = exit_price, exit_time, reason
        eq_times.append(exit_time)
        eq_values.append(equity)

    def finalize_position() -> None:
        nonlocal in_pos
        trades.append(
            {
                "symbol": symbol,
                "entry_time": entry_time,
                "entry_price": entry_price,
                "exit_time": last_exit_time,
                "exit_price": last_exit_price,
                "qty": qty,
                "stop": state.initial_stop,
                "net_pnl": pos_net_pnl,
                "r_multiple": pos_net_pnl / risk_amount if risk_amount > 0 else 0.0,
                "exit_reason": last_reason,
                "equity_after": equity,
            }
        )
        in_pos = False

    for i in range(n - 1):
        nxt = i + 1
        if in_pos:
            assert state is not None
            # A higher-TF trend flip is known at bar i's close -> exit at next open
            # before the bar trades (takes precedence over intrabar management).
            if bool(exit_sig[i]):
                ev = close_remaining(state, float(open_[nxt]), REASON_TREND)
                if ev is not None:
                    apply_fill(ev.fraction, ev.price, times[nxt], ev.reason)
                finalize_position()
                continue
            for ev in evaluate_bar(state, float(high[nxt]), float(low[nxt]), float(atr[nxt]), exit_cfg):
                apply_fill(ev.fraction, ev.price, times[nxt], ev.reason)
            if state.closed:
                finalize_position()
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
            state = BracketState.open(entry_price, stop)
            pos_net_pnl = 0.0
            in_pos = True

    # Close any open position at the final bar's close.
    if in_pos and state is not None:
        ev = close_remaining(state, float(close[-1]), REASON_END)
        if ev is not None:
            apply_fill(ev.fraction, ev.price, times[-1], ev.reason)
        finalize_position()

    trades_df = pd.DataFrame(trades)
    equity_curve = pd.Series(eq_values, index=pd.DatetimeIndex(eq_times), name="equity")
    metrics = compute_metrics(trades_df, equity_curve, initial_equity)
    return BacktestResult(trades=trades_df, equity_curve=equity_curve, metrics=metrics)
