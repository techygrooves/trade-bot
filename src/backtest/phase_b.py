"""Phase B runner: one command that produces the exit-scheme decision report.

Usage (real data; needs data.binance.vision on the network egress allowlist
the first time — candles are cached under --data-dir after that):

  python -m src.backtest.phase_b \
      --symbols BTCUSDT ETHUSDT BNBUSDT SOLUSDT \
      --data-start 2020-10 --data-end 2025-12 \
      --train-start 2021-01-01 --train-end 2023-12-31 \
      --val-start 2024-01-01 --val-end 2025-12-31

  # Re-run later without network (cache hit required):
  python -m src.backtest.phase_b --offline

Data is fetched from a few months BEFORE the train window (--data-start) so
EMAs/ADX are warmed up by the time simulation starts; the extra bars are used
for indicator warm-up only, never simulated.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.config import load_config
from src.logging_setup import setup_logging

from .experiment import GateConfig, Windows, load_symbol, prepare_features, run_experiment
from .report import write_report

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    logger = setup_logging(cfg.settings.log_level)

    p = argparse.ArgumentParser(description="Phase B: validate and choose the exit scheme")
    p.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    p.add_argument("--interval", default=cfg.settings.timeframes.signal)
    p.add_argument("--trend-interval", default=cfg.settings.timeframes.trend)
    p.add_argument("--data-start", default="2020-10",
                   help="first month of candles to fetch, YYYY-MM (before train start, for indicator warm-up)")
    p.add_argument("--data-end", default="2025-12", help="last month of candles to fetch, YYYY-MM")
    p.add_argument("--train-start", default="2021-01-01")
    p.add_argument("--train-end", default="2023-12-31")
    p.add_argument("--val-start", default="2024-01-01")
    p.add_argument("--val-end", default="2025-12-31")
    p.add_argument("--data-dir", default="data", help="candle cache directory")
    p.add_argument("--out", default="reports/phase_b", help="report output directory")
    p.add_argument("--initial", type=float, default=10_000.0)
    p.add_argument("--min-train-trades", type=int, default=GateConfig.min_train_trades)
    p.add_argument("--min-val-trades", type=int, default=GateConfig.min_val_trades)
    p.add_argument("--max-dd-cap", type=float, default=GateConfig.max_dd_cap_pct)
    p.add_argument("--offline", action="store_true",
                   help="never hit the network; fail if candles are not cached")
    p.add_argument("--fetch-only", action="store_true",
                   help="download + cache candles, then exit (no backtests)")
    p.add_argument("--skip-sweep", action="store_true",
                   help="baseline scheme comparison only, no parameter sweep")
    args = p.parse_args(argv)

    windows = Windows(args.train_start, args.train_end, args.val_start, args.val_end)
    gate = GateConfig(args.min_train_trades, args.min_val_trades, args.max_dd_cap)

    feats_by_symbol = {}
    for symbol in args.symbols:
        candles = load_symbol(
            symbol, args.interval, args.data_start, args.data_end,
            Path(args.data_dir), offline=args.offline,
        )
        logger.info("%s: %d %s candles (%s .. %s)", symbol, len(candles),
                    args.interval, candles.index[0], candles.index[-1])
        if not args.fetch_only:
            feats_by_symbol[symbol] = prepare_features(
                candles, args.trend_interval, cfg.settings.strategy
            )
    if args.fetch_only:
        logger.info("Fetch-only run complete; candles cached under %s", args.data_dir)
        return 0

    result = run_experiment(
        feats_by_symbol,
        cfg.settings.strategy,
        cfg.settings.exits,
        cfg.settings.risk,
        windows,
        gate=gate,
        initial_equity=args.initial,
        skip_sweep=args.skip_sweep,
    )
    report_path = write_report(
        result,
        args.out,
        meta={
            "symbols": args.symbols,
            "interval": args.interval,
            "trend_interval": args.trend_interval,
            "windows": windows,
            "risk": cfg.settings.risk,
            "data_source": f"data.binance.vision monthly klines "
                           f"({args.data_start}..{args.data_end}), cached in {args.data_dir}/",
        },
    )
    for note in result.notes:
        logger.info("NOTE: %s", note)
    logger.info("Report written to %s", report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
