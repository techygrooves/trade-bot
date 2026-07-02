"""Tests for the Phase B sweep/holdout validation tooling."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.backtest.validate import (
    Candidate,
    SweepRunner,
    build_grid,
    finalists,
    select,
    slice_window,
)
from src.config import ExitConfig, RiskConfig, StrategyConfig


def test_build_grid_covers_all_schemes_without_duplicates():
    grid = build_grid()
    # 3 adx x 3 atr = 9 strategy points; 1 trend + 3 fixed_tp + 2 scaled exits.
    assert len(grid) == 9 * (1 + 3 + 2)
    by_scheme = pd.Series([c.scheme for c in grid]).value_counts()
    assert by_scheme["trend"] == 9
    assert by_scheme["fixed_tp"] == 27
    assert by_scheme["scaled"] == 18
    labels = [c.label for c in grid]
    assert len(set(labels)) == len(labels)


def test_candidate_overrides_configs():
    c = Candidate("scaled", adx_min=25, atr_stop_mult=1.5, trail_atr_mult=3.0)
    strat = c.strategy_cfg(StrategyConfig())
    assert strat.adx_min == 25 and strat.atr_stop_mult == 1.5
    assert strat.ema_trend == 200  # untouched fields keep their base values
    exits = c.exit_cfg(ExitConfig())
    assert exits.scheme == "scaled" and exits.trail_atr_mult == 3.0


def test_slice_window_is_end_exclusive():
    idx = pd.date_range("2024-01-01", periods=48, freq="1h", tz="UTC")
    feats = pd.DataFrame({"x": range(48)}, index=idx)
    train = slice_window(feats, None, "2024-01-02")
    val = slice_window(feats, "2024-01-02", None)
    assert len(train) + len(val) == 48
    assert train.index.max() < val.index.min()  # no shared boundary bar


def _flat_feats(idx, entries: dict[int, bool], exits: dict[int, bool]) -> pd.DataFrame:
    n = len(idx)
    feats = pd.DataFrame(
        {
            "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
            "atr": 1.0,
            "entry_signal": [entries.get(i, False) for i in range(n)],
            "exit_signal": [exits.get(i, False) for i in range(n)],
        },
        index=idx,
    )
    return feats


def test_runner_confines_trades_to_the_window():
    idx = pd.date_range("2023-12-30", periods=120, freq="1h", tz="UTC")
    split = "2024-01-01"
    split_pos = int((idx < pd.Timestamp(split, tz="UTC")).sum())
    # One round trip before the split, one after.
    feats = _flat_feats(
        idx,
        entries={5: True, split_pos + 10: True},
        exits={15: True, split_pos + 20: True},
    )
    data = {"TEST": (pd.DataFrame(), pd.DataFrame())}  # only keys are used
    runner = SweepRunner(data, StrategyConfig(), RiskConfig(taker_fee_pct=0.0),
                         ExitConfig())
    cand = Candidate("trend", adx_min=20, atr_stop_mult=2.0)
    runner._feature_cache[("TEST", cand.adx_min)] = feats

    train = runner.run(cand, start=None, end=split)
    val = runner.run(cand, start=split, end=None)
    # end_of_data close of the still-open position also counts as one trade,
    # so each window sees exactly its own round trip and nothing else.
    assert train["trades"] == 1
    assert val["trades"] == 1


def test_runner_aggregates_across_symbols():
    idx = pd.date_range("2024-01-01", periods=60, freq="1h", tz="UTC")
    feats = _flat_feats(idx, entries={3: True}, exits={10: True})
    data = {"AAA": (pd.DataFrame(), pd.DataFrame()),
            "BBB": (pd.DataFrame(), pd.DataFrame())}
    runner = SweepRunner(data, StrategyConfig(), RiskConfig(taker_fee_pct=0.0),
                         ExitConfig())
    cand = Candidate("trend", adx_min=20, atr_stop_mult=2.0)
    runner._feature_cache[("AAA", 20.0)] = feats
    runner._feature_cache[("BBB", 20.0)] = feats

    row = runner.run(cand, start=None, end=None)
    assert row["trades"] == 2  # one per symbol
    assert row["label"] == cand.label


def test_select_applies_guardrails_and_ranks_by_total_r():
    rows = pd.DataFrame(
        {
            "label": ["a", "b", "c", "d"],
            "scheme": ["trend", "fixed_tp", "scaled", "fixed_tp"],
            "trades": [50, 5, 40, 60],
            "total_r": [10.0, 99.0, 25.0, 15.0],
            "worst_dd_pct": [20.0, 10.0, 50.0, 30.0],
        }
    )
    ranked = select(rows, min_trades=30, max_dd_cap_pct=35.0)
    # b fails the trade floor, c fails the drawdown cap.
    assert list(ranked["label"]) == ["d", "a"]


def test_finalists_keep_best_per_scheme():
    ranked = pd.DataFrame(
        {
            "label": ["s1", "f1", "s2", "t1", "f2"],
            "scheme": ["scaled", "fixed_tp", "scaled", "trend", "fixed_tp"],
            "total_r": [30.0, 25.0, 20.0, 15.0, 10.0],
        }
    )
    best = finalists(ranked)
    assert len(best) == 3
    assert set(best["label"]) == {"s1", "f1", "t1"}
