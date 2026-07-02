"""Performance metrics for a backtest run."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

SECONDS_PER_YEAR = 365.25 * 24 * 3600


@dataclass
class Metrics:
    n_trades: int
    win_rate: float          # fraction 0..1
    profit_factor: float     # gross profit / gross loss
    expectancy_r: float      # average R per trade
    avg_win_r: float
    avg_loss_r: float
    total_return_pct: float
    max_drawdown_pct: float
    sharpe: float            # annualized, from per-bar equity returns
    avg_hold_hours: float
    final_equity: float

    def as_dict(self) -> dict:
        return self.__dict__.copy()

    def summary(self) -> str:
        return (
            f"trades={self.n_trades} | win_rate={self.win_rate:.1%} | "
            f"profit_factor={self.profit_factor:.2f} | expectancy={self.expectancy_r:.2f}R | "
            f"return={self.total_return_pct:.1f}% | max_dd={self.max_drawdown_pct:.1f}% | "
            f"sharpe={self.sharpe:.2f} | avg_hold={self.avg_hold_hours:.1f}h | "
            f"final_equity={self.final_equity:.2f}"
        )


def max_drawdown_pct(equity_curve: pd.Series) -> float:
    """Largest peak-to-trough decline of the equity curve, as a percentage."""
    if equity_curve.empty:
        return 0.0
    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    return float(-drawdown.min() * 100.0)


def _periods_per_year(index: pd.Index) -> float:
    """Sampling frequency of the curve, inferred from index spacing.

    Falls back to 1.0 (i.e. an unannualized Sharpe) when the index isn't a
    datetime index.
    """
    if isinstance(index, pd.DatetimeIndex) and len(index) > 1:
        spacing = pd.Series(index).diff().median()
        if pd.notna(spacing) and spacing.total_seconds() > 0:
            return SECONDS_PER_YEAR / spacing.total_seconds()
    return 1.0


def sharpe_ratio(equity_curve: pd.Series) -> float:
    """Annualized Sharpe (risk-free rate 0) from per-bar equity returns."""
    rets = equity_curve.pct_change().dropna()
    std = float(rets.std())
    if len(rets) < 2 or std == 0.0 or not np.isfinite(std):
        return 0.0
    return float(rets.mean() / std * np.sqrt(_periods_per_year(equity_curve.index)))


def exit_reason_stats(trades: pd.DataFrame) -> pd.DataFrame:
    """Per-exit-reason breakdown: trade count, win rate, total PnL, average R.

    Trades are bucketed by their FINAL fill's reason (a scaled trade that took
    two partials and then trailed out counts once, under the trailing stop).
    """
    if trades.empty:
        return pd.DataFrame(columns=["trades", "win_rate", "total_pnl", "avg_r"])
    g = trades.groupby("exit_reason")
    return pd.DataFrame(
        {
            "trades": g.size(),
            "win_rate": g["net_pnl"].apply(lambda s: float((s > 0).mean())),
            "total_pnl": g["net_pnl"].sum(),
            "avg_r": g["r_multiple"].mean(),
        }
    )


def compute_metrics(
    trades: pd.DataFrame, equity_curve: pd.Series, initial_equity: float
) -> Metrics:
    """Summarize a list of closed trades and the resulting equity curve."""
    n = len(trades)
    if n == 0:
        return Metrics(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, initial_equity)

    pnl = trades["net_pnl"]
    r = trades["r_multiple"]
    wins = trades[pnl > 0]
    losses = trades[pnl <= 0]

    gross_profit = float(wins["net_pnl"].sum())
    gross_loss = float(-losses["net_pnl"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    final_equity = float(equity_curve.iloc[-1])
    total_return = (final_equity - initial_equity) / initial_equity * 100.0

    avg_hold_hours = 0.0
    if {"entry_time", "exit_time"}.issubset(trades.columns):
        hold = pd.to_datetime(trades["exit_time"]) - pd.to_datetime(trades["entry_time"])
        avg_hold_hours = float(hold.mean().total_seconds() / 3600.0)

    return Metrics(
        n_trades=n,
        win_rate=len(wins) / n,
        profit_factor=profit_factor,
        expectancy_r=float(r.mean()),
        avg_win_r=float(wins["r_multiple"].mean()) if len(wins) else 0.0,
        avg_loss_r=float(losses["r_multiple"].mean()) if len(losses) else 0.0,
        total_return_pct=total_return,
        max_drawdown_pct=max_drawdown_pct(equity_curve),
        sharpe=sharpe_ratio(equity_curve),
        avg_hold_hours=avg_hold_hours,
        final_equity=final_equity,
    )
