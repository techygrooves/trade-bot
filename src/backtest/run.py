"""Run a backtest from the command line.

Examples:
  # From Binance public history (needs data.binance.vision allowlisted):
  python -m src.backtest --symbol BTCUSDT --start 2023-01 --end 2024-12

  # From a local CSV of 1h candles:
  python -m src.backtest --csv data/BTCUSDT-1h.csv
"""
from __future__ import annotations

import argparse

from src.config import load_config
from src.logging_setup import setup_logging

from .data_loader import load_binance_vision, load_csv, resample_trend
from .engine import run_backtest
from .metrics import exit_reason_stats


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
    p.add_argument(
        "--exit-scheme", choices=["trend", "fixed_tp", "scaled"],
        help="override exits.scheme from config",
    )
    p.add_argument(
        "--slippage-bps", type=float,
        help="override risk.slippage_bps from config",
    )
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

    trend_df = resample_trend(signal_df, args.trend_interval)
    logger.info("Loaded %d signal candles, %d trend candles", len(signal_df), len(trend_df))

    exit_cfg = cfg.settings.exits
    if args.exit_scheme:
        exit_cfg = exit_cfg.model_copy(update={"scheme": args.exit_scheme})
    risk_cfg = cfg.settings.risk
    if args.slippage_bps is not None:
        risk_cfg = risk_cfg.model_copy(update={"slippage_bps": args.slippage_bps})

    result = run_backtest(
        signal_df, trend_df, cfg.settings.strategy, risk_cfg, exit_cfg,
        initial_equity=args.initial, symbol=args.symbol,
    )
    logger.info(
        "Exit scheme: %s | slippage: %.1f bps | fee: %.3f%%",
        exit_cfg.scheme, risk_cfg.slippage_bps, risk_cfg.taker_fee_pct,
    )
    logger.info("RESULT: %s", result.metrics.summary())
    if not result.trades.empty:
        logger.info("Per-exit-reason breakdown:\n%s",
                    exit_reason_stats(result.trades).round(3).to_string())


if __name__ == "__main__":
    main()
