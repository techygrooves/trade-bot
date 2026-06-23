"""Technical indicators implemented in pure pandas/numpy.

Hand-rolled (rather than depending on pandas-ta, which is currently broken
against modern numpy) so they are dependency-light and fully unit-testable.

All functions are pure: given the same input Series/DataFrame they return the
same output, with no I/O or global state. This makes the strategy easy to test
and to reuse unchanged in the backtester (Phase 2).

Smoothing convention: RSI, ATR and ADX use Wilder's smoothing (RMA), which is
an EWMA with alpha = 1/period. EMA/MACD use the standard span-based EWMA.
"""
from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (standard span-based)."""
    return series.ewm(span=period, adjust=False).mean()


def _rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's moving average (a.k.a. RMA / SMMA)."""
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder), bounded 0–100."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = _rma(gain, period)
    avg_loss = _rma(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    out = 100.0 - (100.0 / (1.0 + rs))
    # When average loss is zero (pure uptrend), RSI is 100.
    out = out.where(avg_loss != 0.0, 100.0)
    return out.astype(float)


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """MACD line, signal line, and histogram."""
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return pd.DataFrame(
        {"macd": macd_line, "macd_signal": signal_line, "macd_hist": hist}
    )


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True Range."""
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.Series:
    """Average True Range (Wilder)."""
    return _rma(true_range(high, low, close), period)


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.DataFrame:
    """Average Directional Index with +DI / -DI (Wilder).

    Returns columns: adx, plus_di, minus_di.
    """
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move.clip(lower=0.0)
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move.clip(lower=0.0)

    atr_ = _rma(true_range(high, low, close), period)
    plus_di = 100.0 * _rma(plus_dm, period) / atr_
    minus_di = 100.0 * _rma(minus_dm, period) / atr_

    di_sum = plus_di + minus_di
    # Keep float dtype (avoid pd.NA -> object) so the downstream fillna doesn't
    # trigger pandas' deprecated object-downcasting path.
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum.where(di_sum != 0.0)
    adx_ = _rma(dx.fillna(0.0), period)

    return pd.DataFrame(
        {"adx": adx_.astype(float), "plus_di": plus_di, "minus_di": minus_di}
    )
