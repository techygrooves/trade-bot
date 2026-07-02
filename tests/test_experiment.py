"""Tests for the Phase B experiment harness (sweep, gating, aggregation, report)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest import experiment as exp
from src.backtest.data_loader import save_csv
from src.backtest.engine import simulate
from src.backtest.experiment import (
    Combo,
    GateConfig,
    Windows,
    apply_thresholds,
    default_combo,
    gate_pass,
    load_symbol,
    pool_metrics,
    prepare_features,
    rank_key,
    run_experiment,
    sweep_combos,
    window_slice,
)
from src.backtest.report import md_table, write_report
from src.config import ExitConfig, RiskConfig, StrategyConfig
from src.strategy.signals import compute_features


def _ohlcv(n: int = 3000, seed: int = 7, start: str = "2021-01-01") -> pd.DataFrame:
    """Synthetic upward-drifting hourly OHLCV that produces real entries."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0007, 0.012, n)
    price = 100 * np.exp(np.cumsum(rets))
    idx = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": price,
            "high": price * (1 + np.abs(rng.normal(0, 0.004, n))),
            "low": price * (1 - np.abs(rng.normal(0, 0.004, n))),
            "close": price,
            "volume": rng.lognormal(4.5, 0.4, n),
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# apply_thresholds must equal a full compute_features re-run
# ---------------------------------------------------------------------------

def test_apply_thresholds_parity_with_compute_features():
    raw = _ohlcv()
    trend = raw.resample("4h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    base = StrategyConfig()
    feats_base = compute_features(raw, trend, base)

    tweaked = StrategyConfig(
        adx_min=15.0, rsi_lower=35.0, rsi_upper=65.0, atr_stop_mult=1.5
    )
    fast = apply_thresholds(feats_base, tweaked)
    slow = compute_features(raw, trend, tweaked)

    pd.testing.assert_series_equal(fast["entry_signal"], slow["entry_signal"])
    pd.testing.assert_series_equal(fast["adx_ok"], slow["adx_ok"])
    pd.testing.assert_series_equal(fast["rsi_ok"], slow["rsi_ok"])
    pd.testing.assert_series_equal(fast["stop_price"], slow["stop_price"])
    # And the tweak actually changed something (guards against a vacuous test).
    assert fast["entry_signal"].sum() != feats_base["entry_signal"].sum()


# ---------------------------------------------------------------------------
# Windows & slicing
# ---------------------------------------------------------------------------

def test_windows_reject_bad_order():
    with pytest.raises(ValueError, match="ordered"):
        Windows("2024-01-01", "2023-01-01", "2024-06-01", "2024-12-31")
    with pytest.raises(ValueError, match="ordered"):
        Windows("2021-01-01", "2023-12-31", "2023-06-01", "2024-12-31")  # overlap


def test_window_slice_is_inclusive_of_end_day():
    df = _ohlcv(n=24 * 10, start="2021-01-01")  # 10 days hourly
    sliced = window_slice(df, "2021-01-03", "2021-01-05")
    assert sliced.index[0] == pd.Timestamp("2021-01-03 00:00", tz="UTC")
    assert sliced.index[-1] == pd.Timestamp("2021-01-05 23:00", tz="UTC")
    assert window_slice(df, None, None).equals(df)


# ---------------------------------------------------------------------------
# Sweep grid
# ---------------------------------------------------------------------------

def test_sweep_combos_cover_grid_with_unique_labels():
    combos = sweep_combos()
    # 3 adx x 2 rsi bands x 3 stop mults = 18 threshold points per scheme;
    # trend has 1 exit variant, fixed_tp 3, scaled 2.
    assert len([c for c in combos if c.scheme == "trend"]) == 18
    assert len([c for c in combos if c.scheme == "fixed_tp"]) == 54
    assert len([c for c in combos if c.scheme == "scaled"]) == 36
    labels = [c.label() for c in combos]
    assert len(labels) == len(set(labels))


def test_combo_builds_configs():
    c = Combo("fixed_tp", 25.0, 35.0, 65.0, 1.5, {"reward_mult": 3.0})
    strat = c.strategy_cfg(StrategyConfig())
    assert (strat.adx_min, strat.rsi_lower, strat.rsi_upper, strat.atr_stop_mult) == (
        25.0, 35.0, 65.0, 1.5,
    )
    ex = c.exit_cfg(ExitConfig())
    assert ex.scheme == "fixed_tp" and ex.reward_mult == 3.0
    d = default_combo("scaled", StrategyConfig())
    assert d.scheme == "scaled" and d.adx_min == StrategyConfig().adx_min


# ---------------------------------------------------------------------------
# Pooling & the decision gate
# ---------------------------------------------------------------------------

def _result_from_path(rows, exit_cfg=None) -> "object":
    """Run simulate() on a hand-built feature path (same shape as exit tests)."""
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="1h", tz="UTC")
    o, h, l, c, e, x = zip(*rows)
    feats = pd.DataFrame(
        {"open": o, "high": h, "low": l, "close": c,
         "atr": 1.0, "entry_signal": e, "exit_signal": x},
        index=idx,
    )
    return simulate(
        feats, StrategyConfig(), RiskConfig(taker_fee_pct=0.0, slippage_bps=0.0),
        exit_cfg or ExitConfig(scheme="fixed_tp", reward_mult=1.5),
    )


def test_pool_metrics_pools_trades_and_takes_worst_drawdown():
    winner = _result_from_path([
        (100, 100, 100, 100, True, False),
        (100, 105, 99.5, 104, False, False),   # +1.5R take-profit
        (104, 104, 104, 104, False, False),
    ])
    loser = _result_from_path([
        (100, 100, 100, 100, True, False),
        (100, 100.5, 97, 98, False, False),    # -1R stop
        (98, 98, 98, 98, False, False),
    ])
    pooled = pool_metrics({"WIN": winner, "LOSE": loser})
    assert pooled["n_trades"] == 2
    assert pooled["expectancy_r"] == pytest.approx((1.5 - 1.0) / 2)
    assert pooled["win_rate"] == pytest.approx(0.5)
    assert pooled["profit_factor"] == pytest.approx(1.5)
    assert pooled["worst_max_dd_pct"] == pytest.approx(
        max(winner.metrics.max_drawdown_pct, loser.metrics.max_drawdown_pct)
    )


def test_pool_metrics_empty_is_all_zero():
    flat = _result_from_path([(100, 100, 100, 100, False, False)] * 3)
    pooled = pool_metrics({"X": flat})
    assert pooled["n_trades"] == 0
    assert pooled["expectancy_r"] == 0.0


def test_gate_requires_trades_drawdown_and_positive_expectancy():
    gate = GateConfig(min_train_trades=40, min_val_trades=10, max_dd_cap_pct=30.0)
    good = {"n_trades": 50, "worst_max_dd_pct": 20.0, "expectancy_r": 0.3}
    assert gate_pass(good, gate, gate.min_train_trades)
    assert not gate_pass({**good, "n_trades": 39}, gate, gate.min_train_trades)
    assert not gate_pass({**good, "worst_max_dd_pct": 31.0}, gate, gate.min_train_trades)
    assert not gate_pass({**good, "expectancy_r": 0.0}, gate, gate.min_train_trades)


def test_rank_key_ignores_win_rate():
    # Higher expectancy wins even with a much lower win rate.
    low_wr = {"expectancy_r": 0.5, "profit_factor": 2.0, "win_rate": 0.3}
    high_wr = {"expectancy_r": 0.2, "profit_factor": 3.0, "win_rate": 0.9}
    assert rank_key(low_wr) > rank_key(high_wr)
    # Infinite profit factor doesn't blow up the comparison.
    assert rank_key({"expectancy_r": 0.5, "profit_factor": float("inf")}) > rank_key(low_wr)


# ---------------------------------------------------------------------------
# Candle cache
# ---------------------------------------------------------------------------

def test_load_symbol_cache_hit_and_offline_miss(tmp_path):
    df = _ohlcv(n=48)
    df.index.name = "open_time"
    save_csv(df, tmp_path / "BTCUSDT-1h-2021-01-2021-02.csv")
    loaded = load_symbol("BTCUSDT", "1h", "2021-01", "2021-02", tmp_path, offline=True)
    assert len(loaded) == 48
    assert loaded["close"].iloc[-1] == pytest.approx(df["close"].iloc[-1])
    with pytest.raises(FileNotFoundError, match="offline"):
        load_symbol("ETHUSDT", "1h", "2021-01", "2021-02", tmp_path, offline=True)


# ---------------------------------------------------------------------------
# End-to-end experiment + report (tiny grid so it stays fast)
# ---------------------------------------------------------------------------

@pytest.fixture()
def tiny_grid(monkeypatch):
    monkeypatch.setattr(
        exp, "THRESHOLD_GRID",
        {"adx_min": [15.0, 20.0], "rsi_band": [(40.0, 60.0)], "atr_stop_mult": [2.0]},
    )
    monkeypatch.setattr(
        exp, "EXIT_GRID",
        {"trend": [{}], "fixed_tp": [{"reward_mult": 1.5}], "scaled": [{"trail_atr_mult": 2.0}]},
    )


def test_run_experiment_end_to_end(tmp_path, tiny_grid):
    strategy, risk, exits = StrategyConfig(), RiskConfig(), ExitConfig()
    feats = {
        "AAAUSDT": prepare_features(_ohlcv(n=4000, seed=3), "4h", strategy),
        "BBBUSDT": prepare_features(_ohlcv(n=4000, seed=9), "4h", strategy),
    }
    windows = Windows("2021-01-15", "2021-04-15", "2021-04-16", "2021-06-15")
    gate = GateConfig(min_train_trades=1, min_val_trades=1, max_dd_cap_pct=100.0)

    result = run_experiment(feats, strategy, exits, risk, windows, gate=gate)

    # Baseline: 3 schemes x 3 windows.
    assert len(result.baseline) == 9
    assert set(result.baseline["scheme"]) == {"trend", "fixed_tp", "scaled"}
    # Sweep ran on train only, tiny grid = 2 combos per scheme.
    assert len(result.sweep) == 6
    assert (result.sweep["window"] == "train").all()
    # One champion per scheme, each with a train and a validation row.
    assert len(result.champions) == 6
    assert sorted(result.champions["window"].unique()) == ["train", "validation"]
    # The decision is recorded either way.
    assert any(n.startswith("DECISION") for n in result.notes)
    if result.winner is not None:
        assert result.winner["window"] == "validation"
        assert result.winner["gate_pass"]

    report = write_report(
        result, tmp_path / "phase_b",
        meta={
            "symbols": list(feats), "interval": "1h", "trend_interval": "4h",
            "windows": windows, "risk": risk, "data_source": "SYNTHETIC (test)",
        },
    )
    text = report.read_text()
    assert "## Decision" in text and "SYNTHETIC" in text
    assert (tmp_path / "phase_b" / "baseline.csv").exists()
    assert (tmp_path / "phase_b" / "sweep.csv").exists()
    assert (tmp_path / "phase_b" / "champions.csv").exists()


def test_run_experiment_skip_sweep(tiny_grid):
    strategy, risk, exits = StrategyConfig(), RiskConfig(), ExitConfig()
    feats = {"AAAUSDT": prepare_features(_ohlcv(n=3000, seed=3), "4h", strategy)}
    windows = Windows("2021-01-15", "2021-03-15", "2021-03-16", "2021-05-01")
    result = run_experiment(
        feats, strategy, exits, risk, windows,
        gate=GateConfig(1, 1, 100.0), skip_sweep=True,
    )
    assert len(result.baseline) == 9
    assert result.sweep.empty and result.champions.empty and result.winner is None


def test_md_table_renders_without_tabulate():
    df = pd.DataFrame({"a": [1, 2], "b": [0.5, float("inf")]})
    text = md_table(df)
    assert text.splitlines()[0] == "| a | b |"
    assert "inf" in text
    assert md_table(pd.DataFrame()) == "_(no rows)_"
