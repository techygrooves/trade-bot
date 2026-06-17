"""Performance metrics for a backtest run."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


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
    final_equity: float

    def as_dict(self) -> dict:
        return self.__dict__.copy()

    def summary(self) -> str:
        return (
            f"trades={self.n_trades} | win_rate={self.win_rate:.1%} | "
            f"profit_factor={self.profit_factor:.2f} | expectancy={self.expectancy_r:.2f}R | "
            f"return={self.total_return_pct:.1f}% | max_dd={self.max_drawdown_pct:.1f}% | "
            f"final_equity={self.final_equity:.2f}"
        )


def max_drawdown_pct(equity_curve: pd.Series) -> float:
    """Largest peak-to-trough decline of the equity curve, as a percentage."""
    if equity_curve.empty:
        return 0.0
    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    return float(-drawdown.min() * 100.0)


def compute_metrics(
    trades: pd.DataFrame, equity_curve: pd.Series, initial_equity: float
) -> Metrics:
    """Summarize a list of closed trades and the resulting equity curve."""
    n = len(trades)
    if n == 0:
        return Metrics(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, initial_equity)

    pnl = trades["net_pnl"]
    r = trades["r_multiple"]
    wins = trades[pnl > 0]
    losses = trades[pnl <= 0]

    gross_profit = float(wins["net_pnl"].sum())
    gross_loss = float(-losses["net_pnl"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    final_equity = float(equity_curve.iloc[-1])
    total_return = (final_equity - initial_equity) / initial_equity * 100.0

    return Metrics(
        n_trades=n,
        win_rate=len(wins) / n,
        profit_factor=profit_factor,
        expectancy_r=float(r.mean()),
        avg_win_r=float(wins["r_multiple"].mean()) if len(wins) else 0.0,
        avg_loss_r=float(losses["r_multiple"].mean()) if len(losses) else 0.0,
        total_return_pct=total_return,
        max_drawdown_pct=max_drawdown_pct(equity_curve),
        final_equity=final_equity,
    )
