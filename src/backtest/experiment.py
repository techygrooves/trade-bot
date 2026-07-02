"""Phase B experiment harness: validate and choose the exit scheme.

Implements NEXT_STEPS.md Phase B on top of the Phase A engine:

  1. Multi-year, multi-symbol backtests of all three exit schemes
     (trend / fixed_tp / scaled) with the default parameters — the baseline
     comparison.
  2. A small, honest parameter sweep (ADX threshold, RSI band, ATR stop mult,
     reward_mult / trail width) evaluated ONLY on the train window. The best
     combo per scheme is then scored ONCE on the held-out validation window.
  3. A decision gate that picks the winner by pooled expectancy (R), profit
     factor and worst per-symbol max drawdown — never by win rate.

Methodology notes (what keeps this honest):
  * Features are computed on the FULL history, then the simulation is run on
    date windows. Indicators only look backward, so a train-window simulation
    never sees validation prices — but it does get properly warmed-up EMAs
    instead of losing the first 200 bars of each window.
  * Sweep combos vary only threshold parameters (adx_min, RSI band) and
    execution parameters (atr_stop_mult, reward_mult, trail width). Indicator
    periods stay fixed, so indicator columns are computed once per symbol and
    the entry flags are cheaply re-derived per combo (`apply_thresholds`
    mirrors `compute_features` exactly; tests assert parity).
  * Selection happens on train only. Validation numbers are reported for the
    per-scheme champions alone — no cherry-picking the best validation row.
  * A position still open at a window's end is force-closed at the last close
    (`end_of_data`), so windows never leak into each other.
  * Metrics are pooled across symbols: expectancy / profit factor / win rate
    from the pooled trade list, drawdown as the WORST single-symbol drawdown
    (a portfolio netting effect is Phase D's business, not Phase B's).
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src.config import ExitConfig, RiskConfig, StrategyConfig
from src.strategy.signals import compute_features

from .data_loader import load_binance_vision, load_csv, resample_trend, save_csv
from .engine import BacktestResult, simulate

logger = logging.getLogger("bot.backtest.experiment")

SCHEMES = ("trend", "fixed_tp", "scaled")

# Sweep space shared by every scheme: entry thresholds + stop width.
THRESHOLD_GRID: dict[str, list] = {
    "adx_min": [15.0, 20.0, 25.0],
    "rsi_band": [(40.0, 60.0), (35.0, 65.0)],
    "atr_stop_mult": [1.5, 2.0, 2.5],
}

# Scheme-specific exit parameters.
EXIT_GRID: dict[str, list[dict]] = {
    "trend": [{}],
    "fixed_tp": [{"reward_mult": r} for r in (1.5, 2.0, 3.0)],
    "scaled": [{"trail_atr_mult": t} for t in (2.0, 3.0)],
}


@dataclass
class Windows:
    """Date windows for the train/validation split (ISO dates, inclusive)."""

    train_start: str = "2021-01-01"
    train_end: str = "2023-12-31"
    val_start: str = "2024-01-01"
    val_end: str = "2025-12-31"

    def __post_init__(self) -> None:
        order = [self.train_start, self.train_end, self.val_start, self.val_end]
        stamps = [pd.Timestamp(d) for d in order]
        if not all(a < b for a, b in zip(stamps, stamps[1:])):
            raise ValueError(f"windows must be ordered train < validation, got {order}")


@dataclass
class GateConfig:
    """Decision-gate thresholds (see NEXT_STEPS.md Phase B.3)."""

    min_train_trades: int = 40      # pooled across symbols; fewer = no statistical weight
    min_val_trades: int = 10
    max_dd_cap_pct: float = 30.0    # worst single-symbol drawdown allowed


@dataclass
class Combo:
    """One point in the sweep grid."""

    scheme: str
    adx_min: float
    rsi_lower: float
    rsi_upper: float
    atr_stop_mult: float
    exit_params: dict = field(default_factory=dict)

    def label(self) -> str:
        extra = "".join(f" {k}={v}" for k, v in sorted(self.exit_params.items()))
        return (
            f"{self.scheme} adx>{self.adx_min:g} rsi[{self.rsi_lower:g},{self.rsi_upper:g}]"
            f" stop={self.atr_stop_mult:g}xATR{extra}"
        )

    def strategy_cfg(self, base: StrategyConfig) -> StrategyConfig:
        return base.model_copy(
            update={
                "adx_min": self.adx_min,
                "rsi_lower": self.rsi_lower,
                "rsi_upper": self.rsi_upper,
                "atr_stop_mult": self.atr_stop_mult,
            }
        )

    def exit_cfg(self, base: ExitConfig) -> ExitConfig:
        return base.model_copy(update={"scheme": self.scheme, **self.exit_params})


def sweep_combos(schemes: tuple[str, ...] = SCHEMES) -> list[Combo]:
    """Enumerate the full (small, honest) sweep grid."""
    combos = []
    for scheme in schemes:
        for adx, (lo, hi), stop_mult, exit_params in itertools.product(
            THRESHOLD_GRID["adx_min"],
            THRESHOLD_GRID["rsi_band"],
            THRESHOLD_GRID["atr_stop_mult"],
            EXIT_GRID[scheme],
        ):
            combos.append(Combo(scheme, adx, lo, hi, stop_mult, dict(exit_params)))
    return combos


def default_combo(scheme: str, strategy: StrategyConfig) -> Combo:
    """The config.yaml defaults expressed as a Combo (the sweep's baseline)."""
    return Combo(
        scheme=scheme,
        adx_min=strategy.adx_min,
        rsi_lower=strategy.rsi_lower,
        rsi_upper=strategy.rsi_upper,
        atr_stop_mult=strategy.atr_stop_mult,
    )


# ---------------------------------------------------------------------------
# Data loading & feature preparation
# ---------------------------------------------------------------------------

def load_symbol(
    symbol: str,
    interval: str,
    start_month: str,
    end_month: str,
    data_dir: Path,
    offline: bool = False,
) -> pd.DataFrame:
    """Load candles from the local cache, downloading and caching on a miss.

    Cache key includes the range, so extending the range re-downloads rather
    than silently returning a shorter history.
    """
    data_dir = Path(data_dir)
    cache = data_dir / f"{symbol}-{interval}-{start_month}-{end_month}.csv"
    if cache.exists():
        logger.info("%s: using cached candles %s", symbol, cache)
        return load_csv(cache)
    if offline:
        raise FileNotFoundError(
            f"{cache} not cached and --offline given. Either run once with network "
            "access to data.binance.vision, or place a candle CSV at that path."
        )
    logger.info("%s: downloading %s %s..%s from data.binance.vision",
                symbol, interval, start_month, end_month)
    df = load_binance_vision(symbol, interval, start_month, end_month)
    save_csv(df, cache)
    return df


def prepare_features(
    signal_df: pd.DataFrame, trend_interval: str, strategy: StrategyConfig
) -> pd.DataFrame:
    """Full-history feature frame for one symbol (computed once, sliced later)."""
    trend_df = resample_trend(signal_df, trend_interval)
    return compute_features(signal_df, trend_df, strategy)


def apply_thresholds(feats: pd.DataFrame, strategy: StrategyConfig) -> pd.DataFrame:
    """Re-derive the threshold-dependent flags on a precomputed feature frame.

    Must mirror the tail of `compute_features` exactly (tests assert parity):
    only `adx_ok`, `rsi_ok`, `entry_signal` and `stop_price` depend on the
    swept thresholds; every indicator column is threshold-independent.
    """
    out = feats.copy()
    out["adx_ok"] = out["adx"] > strategy.adx_min
    out["rsi_ok"] = (out["rsi"] >= strategy.rsi_lower) & (out["rsi"] <= strategy.rsi_upper)
    out["entry_signal"] = (
        out["trend_bull"]
        & out["uptrend_intact"]
        & out["momentum_ok"]
        & out["adx_ok"]
        & out["rsi_ok"]
        & out["vol_ok"]
    ).fillna(False)
    out["stop_price"] = out["close"] - strategy.atr_stop_mult * out["atr"]
    return out


def window_slice(feats: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    """Slice a feature frame to [start, end] (inclusive, tz-aware safe)."""
    lo = pd.Timestamp(start, tz="UTC") if start else None
    hi = pd.Timestamp(end, tz="UTC") if end else None
    if hi is not None:
        hi = hi + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)  # whole end day
    return feats.loc[lo:hi]


# ---------------------------------------------------------------------------
# Running & aggregating
# ---------------------------------------------------------------------------

def run_combo(
    feats_by_symbol: dict[str, pd.DataFrame],
    combo: Combo,
    base_strategy: StrategyConfig,
    base_exit: ExitConfig,
    risk: RiskConfig,
    start: str | None,
    end: str | None,
    initial_equity: float,
) -> dict[str, BacktestResult]:
    """Simulate one combo over one window for every symbol."""
    strat = combo.strategy_cfg(base_strategy)
    exit_cfg = combo.exit_cfg(base_exit)
    results: dict[str, BacktestResult] = {}
    for symbol, feats in feats_by_symbol.items():
        sliced = window_slice(apply_thresholds(feats, strat), start, end)
        if len(sliced) < 2:
            raise ValueError(f"{symbol}: window [{start}, {end}] has {len(sliced)} bars")
        results[symbol] = simulate(
            sliced, strat, risk, exit_cfg, initial_equity=initial_equity, symbol=symbol
        )
    return results


def pool_metrics(results: dict[str, BacktestResult]) -> dict:
    """Pool per-symbol results into one row of cross-symbol evidence.

    Expectancy / profit factor / win rate come from the pooled trade list;
    drawdown is the worst single symbol's (conservative — no netting credit);
    Sharpe and return are per-symbol means (each symbol ran its own equity).
    """
    trades = pd.concat(
        [r.trades for r in results.values() if not r.trades.empty],
        ignore_index=True,
    ) if any(not r.trades.empty for r in results.values()) else pd.DataFrame()

    if trades.empty:
        return {
            "n_trades": 0, "expectancy_r": 0.0, "profit_factor": 0.0,
            "win_rate": 0.0, "worst_max_dd_pct": 0.0, "mean_sharpe": 0.0,
            "mean_return_pct": 0.0,
        }

    pnl = trades["net_pnl"]
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = float(-pnl[pnl <= 0].sum())
    return {
        "n_trades": int(len(trades)),
        "expectancy_r": float(trades["r_multiple"].mean()),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        "win_rate": float((pnl > 0).mean()),
        "worst_max_dd_pct": max(r.metrics.max_drawdown_pct for r in results.values()),
        "mean_sharpe": float(
            sum(r.metrics.sharpe for r in results.values()) / len(results)
        ),
        "mean_return_pct": float(
            sum(r.metrics.total_return_pct for r in results.values()) / len(results)
        ),
    }


def gate_pass(row: dict, gate: GateConfig, min_trades: int) -> bool:
    return (
        row["n_trades"] >= min_trades
        and row["worst_max_dd_pct"] <= gate.max_dd_cap_pct
        and row["expectancy_r"] > 0
    )


def rank_key(row: dict) -> tuple:
    """Sort key for sweep rows: expectancy first, profit factor as tie-break.

    Win rate is deliberately NOT in the key (NEXT_STEPS.md: never select on
    win rate).
    """
    pf = row["profit_factor"]
    return (row["expectancy_r"], pf if pf != float("inf") else 1e9)


# ---------------------------------------------------------------------------
# The experiment itself
# ---------------------------------------------------------------------------

@dataclass
class ExperimentResult:
    baseline: pd.DataFrame          # scheme x window rows, default params
    sweep: pd.DataFrame             # every combo, train window only
    champions: pd.DataFrame         # best combo per scheme: train + validation rows
    winner: dict | None             # the chosen scheme/params (validation row), or None
    notes: list[str]


def _row(combo: Combo, window_name: str, pooled: dict) -> dict:
    return {
        "scheme": combo.scheme,
        "combo": combo.label(),
        "window": window_name,
        "adx_min": combo.adx_min,
        "rsi_lower": combo.rsi_lower,
        "rsi_upper": combo.rsi_upper,
        "atr_stop_mult": combo.atr_stop_mult,
        **{f"exit_{k}": v for k, v in combo.exit_params.items()},
        **pooled,
    }


def run_experiment(
    feats_by_symbol: dict[str, pd.DataFrame],
    base_strategy: StrategyConfig,
    base_exit: ExitConfig,
    risk: RiskConfig,
    windows: Windows,
    gate: GateConfig | None = None,
    initial_equity: float = 10_000.0,
    skip_sweep: bool = False,
) -> ExperimentResult:
    """Run the full Phase B experiment on prepared feature frames."""
    gate = gate or GateConfig()
    notes: list[str] = []

    def run(combo: Combo, start: str | None, end: str | None) -> dict:
        return pool_metrics(
            run_combo(
                feats_by_symbol, combo, base_strategy, base_exit, risk,
                start, end, initial_equity,
            )
        )

    # --- 1. Baseline: default params, every scheme, every window -------------
    window_defs = {
        "train": (windows.train_start, windows.train_end),
        "validation": (windows.val_start, windows.val_end),
        "full": (windows.train_start, windows.val_end),
    }
    baseline_rows = []
    for scheme in SCHEMES:
        combo = default_combo(scheme, base_strategy)
        for wname, (s, e) in window_defs.items():
            logger.info("baseline: %s on %s", combo.label(), wname)
            baseline_rows.append(_row(combo, wname, run(combo, s, e)))
    baseline = pd.DataFrame(baseline_rows)

    # --- 2. Sweep on TRAIN only ----------------------------------------------
    if skip_sweep:
        sweep = pd.DataFrame()
        champions = pd.DataFrame()
        winner = None
        notes.append("Sweep skipped (--skip-sweep); baseline comparison only.")
        return ExperimentResult(baseline, sweep, champions, winner, notes)

    combos = sweep_combos()
    sweep_rows = []
    for i, combo in enumerate(combos, 1):
        logger.info("sweep %d/%d: %s", i, len(combos), combo.label())
        pooled = run(combo, windows.train_start, windows.train_end)
        pooled["gate_pass"] = gate_pass(pooled, gate, gate.min_train_trades)
        sweep_rows.append(_row(combo, "train", pooled))
    sweep = pd.DataFrame(sweep_rows)

    # --- 3. Champion per scheme, then validation ------------------------------
    champion_rows = []
    for scheme in SCHEMES:
        rows = [r for r in sweep_rows if r["scheme"] == scheme]
        passing = [r for r in rows if r["gate_pass"]]
        pool = passing or rows
        if not passing:
            notes.append(
                f"{scheme}: no combo passed the train gate "
                f"(>= {gate.min_train_trades} trades, dd <= {gate.max_dd_cap_pct}%, "
                f"expectancy > 0); champion is best-effort and NOT trade-ready."
            )
        best = max(pool, key=rank_key)
        combo = next(c for c in combos if c.label() == best["combo"])
        champion_rows.append(best)
        logger.info("champion[%s]: %s -> validation", scheme, combo.label())
        val = run(combo, windows.val_start, windows.val_end)
        val["gate_pass"] = gate_pass(val, gate, gate.min_val_trades)
        champion_rows.append(_row(combo, "validation", val))
    champions = pd.DataFrame(champion_rows)

    # --- 4. Decision gate on VALIDATION ---------------------------------------
    val_rows = [r for r in champion_rows if r["window"] == "validation"]
    eligible = [r for r in val_rows if r["gate_pass"]]
    winner = max(eligible, key=rank_key) if eligible else None
    if winner is None:
        notes.append(
            "DECISION: no scheme passed the validation gate. Do NOT proceed to "
            "Phase C with any of these configs; widen the sweep or revisit the "
            "entry logic."
        )
    else:
        train_row = next(
            r for r in champion_rows
            if r["combo"] == winner["combo"] and r["window"] == "train"
        )
        decay = train_row["expectancy_r"] - winner["expectancy_r"]
        notes.append(
            f"DECISION: {winner['combo']} — validation expectancy "
            f"{winner['expectancy_r']:.2f}R over {winner['n_trades']} trades, "
            f"profit factor {winner['profit_factor']:.2f}, worst drawdown "
            f"{winner['worst_max_dd_pct']:.1f}% (train expectancy was "
            f"{train_row['expectancy_r']:.2f}R; decay {decay:+.2f}R)."
        )
        if winner["expectancy_r"] < train_row["expectancy_r"] / 2:
            notes.append(
                "WARNING: validation expectancy is less than half of train — "
                "likely overfit; treat the winner with suspicion."
            )
    return ExperimentResult(baseline, sweep, champions, winner, notes)
