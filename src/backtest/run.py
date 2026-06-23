"""Run a backtest from the command line.

Examples:
  # From Binance public history (needs data.binance.vision allowlisted):
  python -m src.backtest --symbol BTCUSDT --start 2023-01 --end 2024-12

  # From a local CSV of 1h candles:
  python -m src.backtest --csv data/BTCUSDT-1h.csv
"""
from __future__ import annotations

import argparse

import pandas as pd

from src.config import load_config
from src.logging_setup import setup_logging

from .data_loader import load_binance_vision, load_csv
from .engine import run_backtest

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def _resample(signal_df: pd.DataFrame, trend_interval: str) -> pd.DataFrame:
    """Build the higher-timeframe trend frame by resampling the signal frame."""
    return signal_df.resample(trend_interval).agg(_AGG).dropna()


def main() -> None:
    cfg = load_config()
    logger = setup_logging(cfg.settings.log_level)

    p = argparse.ArgumentParser(description="Backtest the trend-momentum strategy")
    p.add_argument("--symbol", default=cfg.settings.symbols[0])
    p.add_argument("--interval", default=cfg.settings.timeframes.signal)
    p.add_argument("--trend-interval", default=cfg.settings.timeframes.trend)
    p.add_argument("--start", help="start month YYYY-MM (Binance Vision source)")
    p.add_argument("--end", help="end month YYYY-MM (Binance Vision source)")
    p.add_argument("--csv", help="path to a CSV of signal-interval candles")
    p.add_argument("--initial", type=float, default=10_000.0)
    args = p.parse_args()

    if args.csv:
        logger.info("Loading candles from CSV %s", args.csv)
        signal_df = load_csv(args.csv)
    else:
        if not (args.start and args.end):
            p.error("provide --start and --end (YYYY-MM), or --csv")
        logger.info(
            "Downloading %s %s candles %s..%s from data.binance.vision",
            args.symbol, args.interval, args.start, args.end,
        )
        signal_df = load_binance_vision(args.symbol, args.interval, args.start, args.end)

    trend_df = _resample(signal_df, args.trend_interval)
    logger.info("Loaded %d signal candles, %d trend candles", len(signal_df), len(trend_df))

    result = run_backtest(
        signal_df, trend_df, cfg.settings.strategy, cfg.settings.risk,
        initial_equity=args.initial, symbol=args.symbol,
        exit_cfg=cfg.settings.exits,
    )
    logger.info("RESULT: %s", result.metrics.summary())
    if not result.trades.empty:
        by_reason = result.trades["exit_reason"].value_counts().to_dict()
        logger.info("Exit reasons: %s", by_reason)


if __name__ == "__main__":
    main()
