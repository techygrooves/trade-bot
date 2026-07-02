"""Event-driven backtester for the trend-momentum strategy.

Design choices that keep results honest (no lookahead, realistic fills):
  * Decisions on bar i use only data known at i's close (via compute_features).
  * Orders execute at the NEXT bar's open. Market fills (entries, stops, trend
    exits) pay `slippage_bps`; take-profits are limit orders and fill at the
    limit price or better.
  * Stops are gap-aware: if a bar OPENS at or through the stop, the fill is
    the open (worse than the stop), not the stop price.
  * The entry bar's own range is checked — a stop or take-profit can be hit on
    the same bar the position was opened.
  * When one bar's range covers both the stop and a take-profit, the stop is
    assumed to fill first (pessimistic: intrabar ordering is unknowable).
  * Position size comes from the risk model: risk a fixed % of equity, sized
    from the stop distance, never exceeding available cash (spot = no leverage).
  * Taker fees are charged on every fill, entries and partial exits alike.
  * Equity is marked to market every bar, so drawdown and Sharpe include
    open-trade excursions, not just realized PnL at trade closes.

Exit schemes (ExitConfig.scheme) — one flag switches between:
  * "trend":    stop-loss or higher-timeframe trend flip only.
  * "fixed_tp": full exit at reward_mult x the stop distance — matches the
                current live engine, so backtests of this scheme are evidence
                for live behavior.
  * "scaled":   partial take-profits at tp1_r / tp2_r, remainder managed by an
                ATR trailing stop (PLAN.md's target scheme).

Single-symbol, one position at a time. A round trip (entry to flat) is one
trade row; scaled partial fills are aggregated into it (`exit_price` is the
quantity-weighted average, `exit_reason` is the final fill's reason, and
`partial_exits` counts the partial fills before it).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.config import ExitConfig, RiskConfig, StrategyConfig
from src.strategy.signals import compute_features

from .metrics import Metrics, compute_metrics

REQUIRED_COLUMNS = {
    "open", "high", "low", "close", "atr", "entry_signal", "exit_signal",
}


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
    exit_cfg: ExitConfig | None = None,
    initial_equity: float = 10_000.0,
    symbol: str = "ASSET",
) -> BacktestResult:
    """Compute features from raw OHLCV frames and simulate the strategy."""
    feats = compute_features(signal_df, trend_df, strategy_cfg)
    return simulate(
        feats, strategy_cfg, risk_cfg, exit_cfg or ExitConfig(),
        initial_equity=initial_equity, symbol=symbol,
    )


def simulate(
    feats: pd.DataFrame,
    strategy_cfg: StrategyConfig,
    risk_cfg: RiskConfig,
    exit_cfg: ExitConfig,
    initial_equity: float = 10_000.0,
    symbol: str = "ASSET",
) -> BacktestResult:
    """Simulate over a precomputed feature frame (see REQUIRED_COLUMNS).

    Split out from `run_backtest` so tests can drive exact, hand-built price
    paths through the fill logic without having to reverse-engineer inputs
    that trigger the entry confluence.
    """
    missing = REQUIRED_COLUMNS - set(feats.columns)
    if missing:
        raise ValueError(f"feature frame is missing columns: {sorted(missing)}")
    n = len(feats)
    if n < 2:
        raise ValueError("need at least 2 bars to simulate")

    open_ = feats["open"].to_numpy(dtype=float)
    high = feats["high"].to_numpy(dtype=float)
    low = feats["low"].to_numpy(dtype=float)
    close = feats["close"].to_numpy(dtype=float)
    atr = feats["atr"].to_numpy(dtype=float)
    entry_sig = feats["entry_signal"].to_numpy(dtype=bool)
    exit_sig = feats["exit_signal"].to_numpy(dtype=bool)
    times = feats.index

    scheme = exit_cfg.scheme
    fee = risk_cfg.taker_fee_pct / 100.0
    slip = risk_cfg.slippage_bps / 10_000.0
    risk_frac = risk_cfg.risk_per_trade_pct / 100.0

    cash = initial_equity
    in_pos = False
    qty = init_qty = entry_price = entry_cost = 0.0
    stop = init_stop = risk_amount = 0.0
    tp1 = tp2 = 0.0
    tp1_done = tp2_done = True
    partial_exits = 0
    exit_net = exit_gross = qty_sold = 0.0
    entry_time = None

    trades: list[dict] = []
    eq = np.empty(n, dtype=float)
    eq[0] = cash

    def sell(q: float, price: float, t, reason: str) -> None:
        """Fill a sell; when nothing is left, book the completed trade."""
        nonlocal cash, qty, exit_net, exit_gross, qty_sold, partial_exits, in_pos
        q = min(q, qty)
        gross = q * price
        cash += gross * (1.0 - fee)
        exit_net += gross * (1.0 - fee)
        exit_gross += gross
        qty_sold += q
        qty -= q
        if qty > init_qty * 1e-9:
            partial_exits += 1
            return
        qty = 0.0
        net_pnl = exit_net - entry_cost
        trades.append(
            {
                "symbol": symbol,
                "entry_time": entry_time,
                "entry_price": entry_price,
                "exit_time": t,
                "exit_price": exit_gross / qty_sold,
                "qty": init_qty,
                "stop": init_stop,
                "net_pnl": net_pnl,
                "r_multiple": net_pnl / risk_amount if risk_amount > 0 else 0.0,
                "exit_reason": reason,
                "partial_exits": partial_exits,
                "equity_after": cash,
            }
        )
        in_pos = False

    def bar_exits(b: int) -> None:
        """Stop / take-profit handling inside bar b (stop first: pessimistic)."""
        nonlocal tp1_done, tp2_done
        stop_reason = "trail_stop" if stop > init_stop else "stop"
        if open_[b] <= stop:  # gapped through the stop: fill at the open
            sell(qty, open_[b] * (1.0 - slip), times[b], stop_reason)
            return
        if low[b] <= stop:
            sell(qty, stop * (1.0 - slip), times[b], stop_reason)
            return
        if scheme == "fixed_tp" and high[b] >= tp1:
            sell(qty, max(tp1, open_[b]), times[b], "take_profit")
            return
        if scheme == "scaled":
            if not tp1_done and high[b] >= tp1:
                tp1_done = True
                sell(exit_cfg.tp1_frac * init_qty, max(tp1, open_[b]), times[b], "tp1")
            if in_pos and not tp2_done and high[b] >= tp2:
                tp2_done = True
                sell(exit_cfg.tp2_frac * init_qty, max(tp2, open_[b]), times[b], "tp2")

    for i in range(n - 1):
        b = i + 1  # the bar on which decisions made at i's close execute
        if in_pos:
            if exit_sig[i]:
                # Trend flip known at i's close -> market out at b's open.
                sell(qty, open_[b] * (1.0 - slip), times[b], "trend_exit")
            else:
                bar_exits(b)
        elif entry_sig[i] and atr[i] > 0:
            price_in = open_[b] * (1.0 + slip)
            stop0 = price_in - strategy_cfg.atr_stop_mult * atr[i]
            risk_per_unit = price_in - stop0
            if risk_per_unit > 0 and stop0 > 0:
                risk_amount = cash * risk_frac
                q = risk_amount / risk_per_unit
                # Spot: cannot spend more than we have (fees included).
                max_q = cash / (price_in * (1.0 + fee))
                if q > max_q:
                    q = max_q
                    risk_amount = q * risk_per_unit
                if q > 0:
                    qty = init_qty = q
                    entry_price = price_in
                    entry_cost = q * price_in * (1.0 + fee)
                    cash -= entry_cost
                    stop = init_stop = stop0
                    tp1_done = tp2_done = True
                    if scheme == "fixed_tp":
                        tp1 = entry_price + exit_cfg.reward_mult * risk_per_unit
                        tp1_done = False
                    elif scheme == "scaled":
                        tp1 = entry_price + exit_cfg.tp1_r * risk_per_unit
                        tp2 = entry_price + exit_cfg.tp2_r * risk_per_unit
                        tp1_done = tp2_done = False
                    partial_exits = 0
                    exit_net = exit_gross = qty_sold = 0.0
                    entry_time = times[b]
                    in_pos = True
                    bar_exits(b)  # the entry bar itself can hit the stop/TP
        if in_pos and scheme == "scaled":
            # Ratchet the trailing stop at the bar's close; binds from the
            # next bar (this bar's range was already processed above).
            stop = max(stop, close[b] - exit_cfg.trail_atr_mult * atr[b])
        eq[b] = cash + qty * close[b]

    if in_pos:
        sell(qty, close[-1] * (1.0 - slip), times[-1], "end_of_data")
        eq[-1] = cash

    trades_df = pd.DataFrame(trades)
    equity_curve = pd.Series(eq, index=times, name="equity")
    metrics = compute_metrics(trades_df, equity_curve, initial_equity)
    return BacktestResult(trades=trades_df, equity_curve=equity_curve, metrics=metrics)
