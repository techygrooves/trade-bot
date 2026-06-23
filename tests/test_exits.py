"""Tests for the shared exit-policy bracket (stop, scaled TP, breakeven, trail)."""
from __future__ import annotations

from src.config import ExitConfig, TakeProfitLevel
from src.strategy.exit_policy import (
    REASON_STOP,
    REASON_TAKE_PROFIT,
    REASON_TRAILING,
    REASON_TREND,
    BracketState,
    close_remaining,
    evaluate_bar,
    target_price,
)


def _cfg(**kw) -> ExitConfig:
    return ExitConfig(**kw)


def test_open_initializes_risk_and_runner():
    s = BracketState.open(entry_price=100.0, initial_stop=96.0)
    assert s.risk_per_unit == 4.0
    assert s.stop == 96.0
    assert s.remaining == 1.0
    assert not s.trailing_active and not s.closed


def test_target_price():
    s = BracketState.open(100.0, 96.0)
    assert target_price(s, 1.5) == 106.0
    assert target_price(s, 3.0) == 112.0


def test_stop_closes_everything_at_stop_price():
    s = BracketState.open(100.0, 96.0)
    events = evaluate_bar(s, high=99.0, low=95.0, atr=2.0, cfg=_cfg())
    assert len(events) == 1
    assert events[0].reason == REASON_STOP
    assert events[0].price == 96.0
    assert events[0].fraction == 1.0
    assert s.closed


def test_no_trigger_holds_and_tracks_high_water():
    s = BracketState.open(100.0, 96.0)
    events = evaluate_bar(s, high=104.0, low=99.0, atr=2.0, cfg=_cfg())
    assert events == []
    assert s.remaining == 1.0
    assert s.high_water == 104.0
    assert not s.trailing_active


def test_first_take_profit_partials_and_moves_stop_to_breakeven():
    s = BracketState.open(100.0, 96.0)
    events = evaluate_bar(s, high=107.0, low=100.0, atr=2.0, cfg=_cfg())
    assert len(events) == 1
    ev = events[0]
    assert ev.reason == REASON_TAKE_PROFIT
    assert ev.price == 106.0           # +1.5R
    assert abs(ev.fraction - 0.34) < 1e-9
    assert s.levels_hit == 1
    assert s.stop == 100.0             # moved to breakeven
    assert abs(s.remaining - 0.66) < 1e-9
    assert not s.trailing_active


def test_both_rungs_then_trailing_activates():
    s = BracketState.open(100.0, 96.0)
    # High clears both rungs (106 and 112) in one bar.
    events = evaluate_bar(s, high=113.0, low=100.0, atr=2.0, cfg=_cfg())
    reasons = [e.reason for e in events]
    assert reasons == [REASON_TAKE_PROFIT, REASON_TAKE_PROFIT]
    assert abs(sum(e.fraction for e in events) - 0.67) < 1e-9
    assert s.levels_hit == 2
    assert s.trailing_active
    assert abs(s.remaining - 0.33) < 1e-9
    # Trailing stop = high_water(113) - 2*ATR(2) = 109, never below breakeven.
    assert s.stop == 109.0


def test_trailing_stop_exits_runner_next_bar():
    s = BracketState.open(100.0, 96.0)
    evaluate_bar(s, high=113.0, low=100.0, atr=2.0, cfg=_cfg())  # arms trailing, stop=109
    events = evaluate_bar(s, high=110.0, low=108.5, atr=2.0, cfg=_cfg())
    assert len(events) == 1
    assert events[0].reason == REASON_TRAILING
    assert events[0].price == 109.0
    assert abs(events[0].fraction - 0.33) < 1e-9
    assert s.closed


def test_freshly_trailed_stop_not_checked_same_bar():
    # A bar that arms the trail and dips intrabar must NOT stop out on that same
    # bar (that would be lookahead) — only on a subsequent bar.
    s = BracketState.open(100.0, 96.0)
    events = evaluate_bar(s, high=113.0, low=108.0, atr=2.0, cfg=_cfg())
    # Two take-profit fills, but no trailing-stop exit despite low 108 < 109.
    assert [e.reason for e in events] == [REASON_TAKE_PROFIT, REASON_TAKE_PROFIT]
    assert not s.closed


def test_single_full_take_profit_config():
    cfg = _cfg(
        take_profits=[TakeProfitLevel(reward_mult=2.0, size_pct=1.0)],
        trailing_enabled=False,
        breakeven_after_first_tp=False,
    )
    s = BracketState.open(100.0, 96.0)
    events = evaluate_bar(s, high=109.0, low=100.0, atr=2.0, cfg=cfg)  # target 108
    assert len(events) == 1
    assert events[0].reason == REASON_TAKE_PROFIT
    assert events[0].price == 108.0
    assert events[0].fraction == 1.0
    assert s.closed


def test_fraction_never_exceeds_remaining():
    # Rungs summing to >1 must still never close more than 100% of the position.
    cfg = _cfg(
        take_profits=[
            TakeProfitLevel(reward_mult=1.0, size_pct=0.7),
            TakeProfitLevel(reward_mult=2.0, size_pct=0.7),
        ],
        trailing_enabled=False,
    )
    s = BracketState.open(100.0, 96.0)
    events = evaluate_bar(s, high=200.0, low=100.0, atr=2.0, cfg=cfg)
    assert abs(sum(e.fraction for e in events) - 1.0) < 1e-9
    assert s.closed


def test_close_remaining_helper():
    s = BracketState.open(100.0, 96.0)
    ev = close_remaining(s, price=101.0, reason=REASON_TREND)
    assert ev is not None and ev.fraction == 1.0 and ev.price == 101.0
    assert s.closed
    # Closing again is a no-op.
    assert close_remaining(s, 101.0, REASON_TREND) is None


def test_no_events_after_closed():
    s = BracketState.open(100.0, 96.0)
    evaluate_bar(s, high=99.0, low=95.0, atr=2.0, cfg=_cfg())  # stopped out
    assert evaluate_bar(s, high=120.0, low=119.0, atr=2.0, cfg=_cfg()) == []
