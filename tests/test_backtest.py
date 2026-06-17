"""Tests for the backtester engine, metrics, and CSV loader."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.data_loader import load_csv
from src.backtest.engine import run_backtest
from src.backtest.metrics import compute_metrics, max_drawdown_pct
from src.config import RiskConfig, StrategyConfig


def _ohlcv(closes: np.ndarray, volume: np.ndarray | None = None) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    vol = volume if volume is not None else np.full(n, 100.0)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": vol,
        },
        index=idx,
    )


def test_max_drawdown_basic():
    eq = pd.Series([100, 120, 90, 110, 80])
    # Peak 120 -> trough 80 = 33.33% drawdown.
    assert abs(max_drawdown_pct(eq) - (40 / 120 * 100)) < 1e-6


def test_compute_metrics_simple():
    trades = pd.DataFrame(
        {
            "net_pnl": [100.0, -50.0, 200.0, -50.0],
            "r_multiple": [2.0, -1.0, 4.0, -1.0],
        }
    )
    eq = pd.Series([1000, 1100, 1050, 1250, 1200])
    m = compute_metrics(trades, eq, 1000.0)
    assert m.n_trades == 4
    assert abs(m.win_rate - 0.5) < 1e-9
    # gross profit 300 / gross loss 100 = 3.0
    assert abs(m.profit_factor - 3.0) < 1e-9
    assert abs(m.expectancy_r - 1.0) < 1e-9
    assert abs(m.final_equity - 1200.0) < 1e-9


def test_no_trades_is_safe():
    # Flat/choppy data should not crash and yields zero trades.
    flat = np.full(400, 100.0)
    df = _ohlcv(flat)
    res = run_backtest(df, df, StrategyConfig(), RiskConfig())
    assert res.metrics.n_trades == 0
    assert res.metrics.final_equity == 10_000.0


def test_backtest_runs_and_is_consistent():
    rng = np.random.default_rng(11)
    n = 2000
    rets = rng.normal(0.0006, 0.013, n)
    price = 100 * np.exp(np.cumsum(rets))
    idx = pd.date_range("2023-01-01", periods=n, freq="1h", tz="UTC")
    vol = rng.lognormal(4.5, 0.4, n)
    df = pd.DataFrame(
        {
            "open": price,
            "high": price * (1 + np.abs(rng.normal(0, 0.004, n))),
            "low": price * (1 - np.abs(rng.normal(0, 0.004, n))),
            "close": price,
            "volume": vol,
        },
        index=idx,
    )
    res = run_backtest(df, df, StrategyConfig(), RiskConfig())
    # The strategy should have taken at least one trade over a long series.
    assert res.metrics.n_trades >= 1
    # Equity curve and trade ledger agree on the final equity.
    assert abs(res.equity_curve.iloc[-1] - res.metrics.final_equity) < 1e-6
    # Spot-only: equity can never go negative.
    assert (res.equity_curve > 0).all()
    # Every recorded stop exit must sit at or below its entry price.
    stops = res.trades[res.trades["exit_reason"] == "stop"]
    assert (stops["exit_price"] <= stops["entry_price"]).all()


def test_load_csv_roundtrip(tmp_path):
    closes = np.linspace(100, 110, 5)
    df = _ohlcv(closes)
    csv = tmp_path / "candles.csv"
    out = df.reset_index().rename(columns={"index": "open_time"})
    out["open_time"] = (out["open_time"].astype("int64") // 1_000_000)  # ms
    out[["open_time", "open", "high", "low", "close", "volume"]].to_csv(csv, index=False)

    loaded = load_csv(csv)
    assert list(loaded.columns[:5]) == ["open", "high", "low", "close", "volume"]
    assert len(loaded) == 5
    assert abs(loaded["close"].iloc[-1] - 110.0) < 1e-6
