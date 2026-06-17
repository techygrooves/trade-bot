"""Unit tests for the technical indicators."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.indicators import ta


def _series(values) -> pd.Series:
    return pd.Series(values, dtype=float)


def test_ema_tracks_constant_series():
    s = _series([10.0] * 50)
    out = ta.ema(s, 10)
    assert np.isclose(out.iloc[-1], 10.0)


def test_rsi_bounds_and_uptrend():
    rising = _series(np.arange(1, 101, dtype=float))
    r = ta.rsi(rising, 14)
    assert (r.dropna() >= 0).all() and (r.dropna() <= 100).all()
    # A monotonic uptrend should be strongly overbought.
    assert r.iloc[-1] > 95


def test_rsi_downtrend_is_oversold():
    falling = _series(np.arange(100, 0, -1, dtype=float))
    r = ta.rsi(falling, 14)
    assert r.iloc[-1] < 5


def test_atr_positive_and_scales_with_range():
    n = 100
    high = _series(np.full(n, 11.0))
    low = _series(np.full(n, 9.0))
    close = _series(np.full(n, 10.0))
    a = ta.atr(high, low, close, 14)
    assert a.iloc[-1] > 0
    # TR is ~2.0 each bar, so ATR should converge near 2.0.
    assert np.isclose(a.iloc[-1], 2.0, atol=0.1)


def test_adx_high_in_strong_trend():
    n = 120
    base = np.arange(n, dtype=float)
    high = _series(base + 1.0)
    low = _series(base - 1.0)
    close = _series(base)
    out = ta.adx(high, low, close, 14)
    # A clean one-directional trend should produce a high ADX.
    assert out["adx"].iloc[-1] > 40
    assert out["plus_di"].iloc[-1] > out["minus_di"].iloc[-1]


def test_macd_cross_sign():
    # Falling then rising series should flip MACD histogram negative -> positive.
    values = list(np.arange(50, 0, -1, dtype=float)) + list(np.arange(0, 50, dtype=float))
    m = ta.macd(_series(values))
    assert m["macd_hist"].iloc[-1] > 0
