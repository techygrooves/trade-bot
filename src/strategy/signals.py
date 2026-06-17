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


def _trend_close_times(trend: pd.DataFrame) -> pd.Series:
    """When each higher-TF bar's values become known (its close).

    Uses an explicit `close_time` column if present, otherwise infers the bar
    duration from the index spacing. This is what lets us align the trend frame
    to the signal frame without lookahead: a trend bar's indicators are only
    usable on signal bars that open at or after the trend bar has closed.
    """
    if "close_time" in trend.columns:
        return pd.to_datetime(trend["close_time"])
    idx = pd.Series(trend.index)
    freq = idx.diff().median()
    return idx + freq


def compute_features(
    signal_df: pd.DataFrame, trend_df: pd.DataFrame, cfg: StrategyConfig
) -> pd.DataFrame:
    """Compute indicators + every entry/exit condition for the whole series.

    Single source of truth for the strategy: both `generate_signal` (live, last
    row) and the backtester (every row) consume this, so the rules can never
    drift between live and simulated trading.

    Higher-TF trend flags are aligned onto the signal timeframe by `merge_asof`
    on the trend bars' close times, so no future information leaks in.
    """
    sig = add_signal_indicators(signal_df, cfg)
    trend = add_trend_indicators(trend_df, cfg)

    # Build trend flags keyed by close time, on a clean index (keep tz-aware
    # datetimes intact — going through .values would strip the timezone and
    # break the merge against the tz-aware signal index).
    trend_flags = pd.DataFrame(
        {
            "trend_bull": (
                (trend["close"] > trend["ema_trend"])
                & (trend["ema_slow"] > trend["ema_trend"])
            ).reset_index(drop=True),
            "trend_bear": (trend["ema_slow"] < trend["ema_trend"]).reset_index(drop=True),
            "_trend_close": _trend_close_times(trend).reset_index(drop=True),
        }
    ).sort_values("_trend_close")

    left = sig.sort_index()
    feats = pd.merge_asof(
        left,
        trend_flags,
        left_index=True,
        right_on="_trend_close",
        direction="backward",
    )
    feats.index = left.index  # merge_asof drops the index; restore it
    feats = feats.drop(columns=["_trend_close"])
    feats[["trend_bull", "trend_bear"]] = (
        feats[["trend_bull", "trend_bear"]].fillna(False).astype(bool)
    )

    feats["uptrend_intact"] = feats["ema_fast"] > feats["ema_slow"]
    feats["ema_reclaim_up"] = (feats["close"].shift(1) <= feats["ema_fast"].shift(1)) & (
        feats["close"] > feats["ema_fast"]
    )
    feats["macd_cross_up"] = (
        feats["macd"].shift(1) <= feats["macd_signal"].shift(1)
    ) & (feats["macd"] > feats["macd_signal"])
    feats["momentum_ok"] = feats["ema_reclaim_up"] | feats["macd_cross_up"]
    feats["adx_ok"] = feats["adx"] > cfg.adx_min
    feats["rsi_ok"] = (feats["rsi"] >= cfg.rsi_lower) & (feats["rsi"] <= cfg.rsi_upper)
    feats["vol_ok"] = feats["volume"] >= feats["vol_ma"]

    feats["entry_signal"] = (
        feats["trend_bull"]
        & feats["uptrend_intact"]
        & feats["momentum_ok"]
        & feats["adx_ok"]
        & feats["rsi_ok"]
        & feats["vol_ok"]
    ).fillna(False)
    feats["exit_signal"] = feats["trend_bear"]
    feats["stop_price"] = feats["close"] - cfg.atr_stop_mult * feats["atr"]
    return feats


def generate_signal(
    symbol: str,
    signal_df: pd.DataFrame,
    trend_df: pd.DataFrame,
    cfg: StrategyConfig,
) -> SignalResult:
    """Evaluate the latest closed candle and return a decision.

    `signal_df` / `trend_df` are raw OHLCV frames; indicators are computed here
    via `compute_features`, so this matches the backtester exactly.
    """
    feats = compute_features(signal_df, trend_df, cfg)
    last = feats.iloc[-1]
    price = float(last["close"])
    atr_val = float(last["atr"])
    ts = feats.index[-1]
    stop = float(last["stop_price"]) if pd.notna(last["stop_price"]) else None

    # Exit takes priority: if the higher-TF trend has turned down, get out.
    if bool(last["exit_signal"]):
        return SignalResult(
            action=Action.EXIT, symbol=symbol, price=price, atr=atr_val,
            stop_price=stop, timestamp=ts,
            reasons=["higher-timeframe trend bearish (EMA_slow < EMA_trend)"],
        )

    if bool(last["entry_signal"]):
        triggers = []
        if bool(last["ema_reclaim_up"]):
            triggers.append("EMA reclaim")
        if bool(last["macd_cross_up"]):
            triggers.append("MACD cross up")
        return SignalResult(
            action=Action.BUY, symbol=symbol, price=price, atr=atr_val,
            stop_price=stop, timestamp=ts,
            reasons=[f"trend up, {' & '.join(triggers)}, "
                     f"ADX {last['adx']:.1f}, RSI {last['rsi']:.1f}, volume confirmed"],
        )

    reasons: list[str] = []
    if not bool(last["trend_bull"]):
        reasons.append("trend filter not bullish")
    if not bool(last["uptrend_intact"]):
        reasons.append("signal-TF uptrend not intact (EMA_fast <= EMA_slow)")
    if not bool(last["momentum_ok"]):
        reasons.append("no momentum trigger (EMA reclaim / MACD cross)")
    if not bool(last["adx_ok"]):
        reasons.append(f"ADX {last['adx']:.1f} <= {cfg.adx_min}")
    if not bool(last["rsi_ok"]):
        reasons.append(f"RSI {last['rsi']:.1f} outside [{cfg.rsi_lower}, {cfg.rsi_upper}]")
    if not bool(last["vol_ok"]):
        reasons.append("volume below average")

    return SignalResult(
        action=Action.HOLD, symbol=symbol, price=price, atr=atr_val,
        stop_price=stop, timestamp=ts, reasons=reasons,
    )
