"""Trend + momentum signal engine.

Pure functions that turn OHLCV into a trading decision using multi-timeframe
confluence (see PLAN.md):

  Entry (BUY) — all must hold on the latest closed candle:
    * Trend (higher TF): close > EMA200 AND EMA50 > EMA200
    * Uptrend intact (signal TF): EMA_fast > EMA_slow
    * Momentum trigger (signal TF): price reclaims EMA_fast after a pullback
      (close crosses back above EMA_fast), OR MACD bullish cross
    * Strength: ADX > adx_min
    * Pullback: rsi_lower <= RSI <= rsi_upper
    * Volume: volume >= rolling mean volume

  The reclaim-of-EMA_fast trigger (rather than a lagging EMA_fast/EMA_slow
  crossover) is what makes the RSI 40–60 pullback band reachable: it fires as
  price resumes up out of a dip, not after an extended rally.

  Exit (EXIT): higher-TF trend flips bearish (EMA50 < EMA200).

  Otherwise: HOLD.

The engine emits both BUY and EXIT; whether to act depends on whether a
position is open, which the execution/portfolio layer (later phases) decides.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

from src.config import StrategyConfig
from src.indicators import ta


class Action(str, Enum):
    BUY = "BUY"
    EXIT = "EXIT"
    HOLD = "HOLD"


@dataclass
class SignalResult:
    action: Action
    symbol: str
    price: float
    atr: float
    stop_price: float | None
    timestamp: pd.Timestamp
    reasons: list[str] = field(default_factory=list)


def add_signal_indicators(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Attach the signal-timeframe indicator columns to a copy of `df`."""
    out = df.copy()
    out["ema_fast"] = ta.ema(out["close"], cfg.ema_fast)
    out["ema_slow"] = ta.ema(out["close"], cfg.ema_slow)
    out["rsi"] = ta.rsi(out["close"], cfg.rsi_period)
    out["atr"] = ta.atr(out["high"], out["low"], out["close"], cfg.atr_period)
    out[["macd", "macd_signal", "macd_hist"]] = ta.macd(
        out["close"], cfg.macd_fast, cfg.macd_slow, cfg.macd_signal
    )
    out[["adx", "plus_di", "minus_di"]] = ta.adx(
        out["high"], out["low"], out["close"], cfg.adx_period
    )
    out["vol_ma"] = out["volume"].rolling(cfg.vol_ma_period).mean()
    return out


def add_trend_indicators(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Attach the higher-timeframe trend-filter columns to a copy of `df`."""
    out = df.copy()
    out["ema_slow"] = ta.ema(out["close"], cfg.ema_slow)
    out["ema_trend"] = ta.ema(out["close"], cfg.ema_trend)
    return out


def _trend_is_bullish(trend_df: pd.DataFrame) -> bool:
    last = trend_df.iloc[-1]
    return bool(last["close"] > last["ema_trend"] and last["ema_slow"] > last["ema_trend"])


def _trend_is_bearish(trend_df: pd.DataFrame) -> bool:
    last = trend_df.iloc[-1]
    return bool(last["ema_slow"] < last["ema_trend"])


def generate_signal(
    symbol: str,
    signal_df: pd.DataFrame,
    trend_df: pd.DataFrame,
    cfg: StrategyConfig,
) -> SignalResult:
    """Evaluate the latest closed candle and return a decision.

    `signal_df` / `trend_df` are raw OHLCV frames; indicators are computed here.
    """
    sig = add_signal_indicators(signal_df, cfg)
    trend = add_trend_indicators(trend_df, cfg)

    last = sig.iloc[-1]
    prev = sig.iloc[-2]
    price = float(last["close"])
    atr_val = float(last["atr"])
    ts = sig.index[-1]
    stop = price - cfg.atr_stop_mult * atr_val if pd.notna(atr_val) else None

    # Exit takes priority: if the higher-TF trend has turned down, get out.
    if _trend_is_bearish(trend):
        return SignalResult(
            action=Action.EXIT, symbol=symbol, price=price, atr=atr_val,
            stop_price=stop, timestamp=ts,
            reasons=["higher-timeframe trend bearish (EMA_slow < EMA_trend)"],
        )

    reasons: list[str] = []

    trend_ok = _trend_is_bullish(trend)
    if not trend_ok:
        reasons.append("trend filter not bullish")

    uptrend_intact = bool(last["ema_fast"] > last["ema_slow"])
    if not uptrend_intact:
        reasons.append("signal-TF uptrend not intact (EMA_fast <= EMA_slow)")

    # Price reclaims the fast EMA after dipping below it: a pullback resumption.
    ema_reclaim_up = bool(
        prev["close"] <= prev["ema_fast"] and last["close"] > last["ema_fast"]
    )
    macd_cross_up = bool(
        prev["macd"] <= prev["macd_signal"] and last["macd"] > last["macd_signal"]
    )
    momentum_ok = ema_reclaim_up or macd_cross_up
    if not momentum_ok:
        reasons.append("no momentum trigger (EMA reclaim / MACD cross)")

    adx_ok = bool(pd.notna(last["adx"]) and last["adx"] > cfg.adx_min)
    if not adx_ok:
        reasons.append(f"ADX {last['adx']:.1f} <= {cfg.adx_min}")

    rsi_ok = bool(pd.notna(last["rsi"]) and cfg.rsi_lower <= last["rsi"] <= cfg.rsi_upper)
    if not rsi_ok:
        reasons.append(f"RSI {last['rsi']:.1f} outside [{cfg.rsi_lower}, {cfg.rsi_upper}]")

    vol_ok = bool(pd.notna(last["vol_ma"]) and last["volume"] >= last["vol_ma"])
    if not vol_ok:
        reasons.append("volume below average")

    if trend_ok and uptrend_intact and momentum_ok and adx_ok and rsi_ok and vol_ok:
        triggers = []
        if ema_reclaim_up:
            triggers.append("EMA reclaim")
        if macd_cross_up:
            triggers.append("MACD cross up")
        return SignalResult(
            action=Action.BUY, symbol=symbol, price=price, atr=atr_val,
            stop_price=stop, timestamp=ts,
            reasons=[f"trend up, {' & '.join(triggers)}, "
                     f"ADX {last['adx']:.1f}, RSI {last['rsi']:.1f}, volume confirmed"],
        )

    return SignalResult(
        action=Action.HOLD, symbol=symbol, price=price, atr=atr_val,
        stop_price=stop, timestamp=ts, reasons=reasons,
    )
