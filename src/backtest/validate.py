"""Phase B validation: compare exit schemes and tune parameters honestly.

Methodology
-----------
* Holdout split: the parameter sweep may only look at the TRAIN window to rank
  configurations. The VALIDATION window is evaluated once, for the best
  candidate per exit scheme, and those out-of-sample numbers make the
  ship/no-ship decision.
* Features are computed over the full history (indicators only ever look
  backward) and then sliced to the window, so indicator warmup never distorts
  a window's first bars and no future data leaks in.
* Each symbol is simulated independently with its own bankroll; a
  configuration's row aggregates its trades across symbols.
* Guardrails before ranking: a configuration must produce at least
  `min_trades` train trades across symbols, and every symbol's max drawdown
  must stay at or under `max_dd_cap_pct`. Survivors are ranked by TOTAL R
  (sum of trade R-multiples) — profit in risk units, deliberately not win
  rate (see PLAN.md §1).

CLI
---
  python -m src.backtest.validate --start 2021-01 --end 2025-06 \\
      --split 2024-01-01 --out logs/phase_b

Add ``--csv-dir DIR`` (files named ``{SYMBOL}-{interval}.csv``) to run from
local candles instead of downloading from data.binance.vision.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from itertools import product
from pathlib import Path

import pandas as pd

from src.config import ExitConfig, RiskConfig, StrategyConfig, load_config
from src.logging_setup import setup_logging
from src.strategy.signals import compute_features

from .data_loader import load_binance_vision, load_csv
from .engine import simulate
from .metrics import max_drawdown_pct

logger = logging.getLogger("bot.validate")

# The sweep grid, kept deliberately small: every added dimension multiplies
# the chance of curve-fitting the train window.
ADX_MIN_VALUES = (15.0, 20.0, 25.0)
ATR_STOP_VALUES = (1.5, 2.0, 2.5)
REWARD_MULT_VALUES = (1.5, 2.0, 3.0)   # fixed_tp only
TRAIL_ATR_VALUES = (2.0, 3.0)          # scaled only


@dataclass(frozen=True)
class Candidate:
    """One point in the sweep grid."""

    scheme: str
    adx_min: float
    atr_stop_mult: float
    reward_mult: float | None = None     # fixed_tp
    trail_atr_mult: float | None = None  # scaled

    @property
    def label(self) -> str:
        extra = ""
        if self.scheme == "fixed_tp":
            extra = f",tp={self.reward_mult}R"
        elif self.scheme == "scaled":
            extra = f",trail={self.trail_atr_mult}xATR"
        return f"{self.scheme}(adx>{self.adx_min:g},stop={self.atr_stop_mult:g}xATR{extra})"

    def strategy_cfg(self, base: StrategyConfig) -> StrategyConfig:
        return base.model_copy(
            update={"adx_min": self.adx_min, "atr_stop_mult": self.atr_stop_mult}
        )

    def exit_cfg(self, base: ExitConfig) -> ExitConfig:
        update: dict = {"scheme": self.scheme}
        if self.reward_mult is not None:
            update["reward_mult"] = self.reward_mult
        if self.trail_atr_mult is not None:
            update["trail_atr_mult"] = self.trail_atr_mult
        return base.model_copy(update=update)


def slice_window(
    feats: pd.DataFrame, start: str | None, end: str | None
) -> pd.DataFrame:
    """Rows of `feats` with index in [start, end) — end-exclusive, so a train
    window ending at the split and a validation window starting there never
    share a bar."""
    idx = feats.index
    tz = getattr(idx, "tz", None)

    def _ts(value: str) -> pd.Timestamp:
        t = pd.Timestamp(value)
        return t.tz_localize(tz) if tz is not None and t.tz is None else t

    mask = pd.Series(True, index=idx)
    if start is not None:
        mask &= idx >= _ts(start)
    if end is not None:
        mask &= idx < _ts(end)
    return feats.loc[mask]


def build_grid(
    adx_values=ADX_MIN_VALUES,
    atr_values=ATR_STOP_VALUES,
    reward_values=REWARD_MULT_VALUES,
    trail_values=TRAIL_ATR_VALUES,
) -> list[Candidate]:
    grid: list[Candidate] = []
    for adx, atr in product(adx_values, atr_values):
        grid.append(Candidate("trend", adx, atr))
        grid.extend(
            Candidate("fixed_tp", adx, atr, reward_mult=r) for r in reward_values
        )
        grid.extend(
            Candidate("scaled", adx, atr, trail_atr_mult=t) for t in trail_values
        )
    return grid


class SweepRunner:
    """Evaluates grid candidates over a set of symbols with feature caching.

    Entry/exit signals depend (within the sweep grid) only on `adx_min`, so
    features are computed once per (symbol, adx_min) and reused for every
    stop/exit variant — the expensive indicator pass runs |symbols| x |adx|
    times instead of once per candidate.
    """

    def __init__(
        self,
        data: dict[str, tuple[pd.DataFrame, pd.DataFrame]],
        base_strategy: StrategyConfig,
        risk: RiskConfig,
        base_exits: ExitConfig,
    ) -> None:
        self.data = data
        self.base_strategy = base_strategy
        self.risk = risk
        self.base_exits = base_exits
        self._feature_cache: dict[tuple[str, float], pd.DataFrame] = {}

    def _features(self, symbol: str, candidate: Candidate) -> pd.DataFrame:
        key = (symbol, candidate.adx_min)
        if key not in self._feature_cache:
            signal_df, trend_df = self.data[symbol]
            cfg = candidate.strategy_cfg(self.base_strategy)
            self._feature_cache[key] = compute_features(signal_df, trend_df, cfg)
        return self._feature_cache[key]

    def run(
        self, candidate: Candidate, start: str | None, end: str | None
    ) -> dict:
        """Evaluate one candidate over [start, end); aggregate across symbols."""
        all_trades: list[pd.DataFrame] = []
        worst_dd = 0.0
        returns = []
        for symbol in self.data:
            feats = self._features(symbol, candidate)
            window = slice_window(feats, start, end)
            if len(window) < 2:
                continue
            res = simulate(
                window,
                candidate.strategy_cfg(self.base_strategy),
                self.risk,
                candidate.exit_cfg(self.base_exits),
                symbol=symbol,
            )
            if not res.trades.empty:
                all_trades.append(res.trades)
            worst_dd = max(worst_dd, max_drawdown_pct(res.equity_curve))
            returns.append(res.metrics.total_return_pct)

        row = {
            "label": candidate.label,
            "scheme": candidate.scheme,
            "adx_min": candidate.adx_min,
            "atr_stop_mult": candidate.atr_stop_mult,
            "reward_mult": candidate.reward_mult,
            "trail_atr_mult": candidate.trail_atr_mult,
            "trades": 0,
            "total_r": 0.0,
            "expectancy_r": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "worst_dd_pct": worst_dd,
            "avg_return_pct": sum(returns) / len(returns) if returns else 0.0,
        }
        if all_trades:
            trades = pd.concat(all_trades, ignore_index=True)
            pnl = trades["net_pnl"]
            gross_win = float(pnl[pnl > 0].sum())
            gross_loss = float(-pnl[pnl <= 0].sum())
            row.update(
                trades=len(trades),
                total_r=float(trades["r_multiple"].sum()),
                expectancy_r=float(trades["r_multiple"].mean()),
                win_rate=float((pnl > 0).mean()),
                profit_factor=gross_win / gross_loss if gross_loss > 0 else float("inf"),
            )
        return row


def select(
    rows: pd.DataFrame, min_trades: int, max_dd_cap_pct: float
) -> pd.DataFrame:
    """Apply guardrails, then rank by total R earned (best first)."""
    ok = rows[(rows["trades"] >= min_trades) & (rows["worst_dd_pct"] <= max_dd_cap_pct)]
    return ok.sort_values("total_r", ascending=False).reset_index(drop=True)


def finalists(ranked: pd.DataFrame) -> pd.DataFrame:
    """Best surviving candidate per exit scheme (ranked input)."""
    return ranked.groupby("scheme", sort=False).head(1).reset_index(drop=True)


_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}

_REPORT_COLS = [
    "label", "trades", "total_r", "expectancy_r", "win_rate",
    "profit_factor", "worst_dd_pct", "avg_return_pct",
]


def _load_symbol(
    symbol: str, interval: str, trend_interval: str, args
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if args.csv_dir:
        signal_df = load_csv(Path(args.csv_dir) / f"{symbol}-{interval}.csv")
    else:
        signal_df = load_binance_vision(
            symbol, interval, args.start, args.end, cache_dir=args.cache_dir
        )
    trend_df = signal_df.resample(trend_interval).agg(_AGG).dropna()
    return signal_df, trend_df


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.settings.log_level)

    p = argparse.ArgumentParser(description="Sweep + holdout validation (Phase B)")
    p.add_argument("--symbols", nargs="+",
                   default=["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"])
    p.add_argument("--interval", default=cfg.settings.timeframes.signal)
    p.add_argument("--trend-interval", default=cfg.settings.timeframes.trend)
    p.add_argument("--start", default="2021-01", help="history start month YYYY-MM")
    p.add_argument("--end", default="2025-06", help="history end month YYYY-MM")
    p.add_argument("--split", default="2024-01-01",
                   help="validation starts here; train is everything before")
    p.add_argument("--min-trades", type=int, default=30,
                   help="train-window trade floor across symbols")
    p.add_argument("--max-dd", type=float, default=35.0,
                   help="per-symbol train max-drawdown cap, %%")
    p.add_argument("--csv-dir", help="load {SYMBOL}-{interval}.csv from here "
                                     "instead of data.binance.vision")
    p.add_argument("--cache-dir", default="data/cache",
                   help="on-disk cache for downloaded months")
    p.add_argument("--out", default="logs/phase_b",
                   help="prefix for <out>_train.csv / <out>_validation.csv")
    args = p.parse_args()

    data = {}
    for symbol in args.symbols:
        logger.info("Loading %s %s candles...", symbol, args.interval)
        data[symbol] = _load_symbol(symbol, args.interval, args.trend_interval, args)
        logger.info("  %d signal bars (%s .. %s)", len(data[symbol][0]),
                    data[symbol][0].index[0], data[symbol][0].index[-1])

    runner = SweepRunner(data, cfg.settings.strategy, cfg.settings.risk,
                         cfg.settings.exits)
    grid = build_grid()
    logger.info("Sweeping %d candidates on train window (.. %s)", len(grid), args.split)

    train_rows = pd.DataFrame(
        [runner.run(c, start=None, end=args.split) for c in grid]
    )
    ranked = select(train_rows, args.min_trades, args.max_dd)
    logger.info(
        "TRAIN: %d/%d candidates survive guardrails (>=%d trades, dd<=%.0f%%). Top 10:\n%s",
        len(ranked), len(grid), args.min_trades, args.max_dd,
        ranked[_REPORT_COLS].head(10).round(3).to_string(index=False),
    )

    final = finalists(ranked)
    if final.empty:
        logger.error("No candidate survived the guardrails; relax them or widen the grid.")
        return

    logger.info("VALIDATION (%s ..), one shot for the best of each scheme:", args.split)
    val_rows = []
    for _, row in final.iterrows():
        cand = Candidate(
            scheme=row["scheme"], adx_min=row["adx_min"],
            atr_stop_mult=row["atr_stop_mult"],
            reward_mult=None if pd.isna(row["reward_mult"]) else row["reward_mult"],
            trail_atr_mult=None if pd.isna(row["trail_atr_mult"]) else row["trail_atr_mult"],
        )
        val_rows.append(runner.run(cand, start=args.split, end=None))
    val_df = pd.DataFrame(val_rows)
    logger.info("\n%s", val_df[_REPORT_COLS].round(3).to_string(index=False))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    train_rows.to_csv(f"{out}_train.csv", index=False)
    val_df.to_csv(f"{out}_validation.csv", index=False)
    logger.info("Wrote %s_train.csv and %s_validation.csv", out, out)


if __name__ == "__main__":
    main()
