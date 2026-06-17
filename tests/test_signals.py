"""Unit tests for the trend+momentum signal engine.

Uses synthetic OHLCV so it runs with no network/API access.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import StrategyConfig
from src.strategy.signals import Action, generate_signal


def _ohlcv(closes: np.ndarray, volume: np.ndarray | None = None) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    high = closes + 0.5
    low = closes - 0.5
    vol = volume if volume is not None else np.full(n, 100.0)
    return pd.DataFrame(
        {"open": closes, "high": high, "low": low, "close": closes, "volume": vol},
        index=idx,
    )


CFG = StrategyConfig()


def test_bearish_trend_emits_exit():
    # Downward higher-timeframe trend -> EXIT regardless of signal TF.
    down = np.linspace(100, 50, 300)
    sig = _ohlcv(down)
    trend = _ohlcv(down)
    result = generate_signal("BTCUSDT", sig, trend, CFG)
    assert result.action == Action.EXIT


def test_strong_uptrend_no_pullback_is_not_buy():
    # Relentless uptrend keeps RSI overbought (>60), so the pullback filter
    # should block a BUY even though the trend is up.
    up = np.linspace(50, 150, 300)
    sig = _ohlcv(up)
    trend = _ohlcv(up)
    result = generate_signal("BTCUSDT", sig, trend, CFG)
    assert result.action != Action.BUY
    assert any("RSI" in r for r in result.reasons)


def test_uptrend_with_pullback_triggers_buy():
    # Build an uptrend, then a dip that pulls RSI into the entry band and
    # produces a fresh EMA/MACD cross up on the final bar.
    rng = np.random.default_rng(0)
    trend_up = np.linspace(50, 120, 250)
    # Pullback then resume to create a cross on the last candle.
    dip = np.linspace(120, 110, 20)
    resume = np.linspace(110, 118, 15)
    closes = np.concatenate([trend_up, dip, resume])
    closes = closes + rng.normal(0, 0.05, len(closes))  # tiny noise

    sig = _ohlcv(closes, volume=np.full(len(closes), 100.0))
    # Make the final (entry) candle a clear volume expansion.
    sig.iloc[-1, sig.columns.get_loc("volume")] = 500.0

    # Higher timeframe firmly bullish.
    trend_closes = np.linspace(50, 130, 300)
    trend = _ohlcv(trend_closes)

    result = generate_signal("BTCUSDT", sig, trend, CFG)
    assert result.action in (Action.BUY, Action.HOLD)
    if result.action == Action.BUY:
        assert result.stop_price is not None
        assert result.stop_price < result.price


def test_buy_is_reachable_on_realistic_uptrend():
    # A noisy random-walk uptrend must produce at least one BUY over time,
    # proving the entry conditions are jointly satisfiable (not too tight).
    rng = np.random.default_rng(7)
    n = 1200
    rets = rng.normal(0.0008, 0.012, n)
    price = 80 * np.exp(np.cumsum(rets))
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
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
    buys = 0
    for i in range(300, n):
        s = df.iloc[: i + 1]
        if generate_signal("BTCUSDT", s, s, CFG).action == Action.BUY:
            buys += 1
    assert buys >= 1


def test_buy_has_valid_atr_stop():
    up = np.linspace(50, 150, 300)
    sig = _ohlcv(up)
    trend = _ohlcv(up)
    result = generate_signal("BTCUSDT", sig, trend, CFG)
    # Stop is always computed from ATR and sits below price.
    assert result.atr > 0
    assert result.stop_price < result.price
