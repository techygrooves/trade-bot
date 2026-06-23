"""Shared trade-management ("bracket") engine.

This is the single source of truth for how an OPEN long position is exited, used
identically by the backtester and the live trader so their results can't drift.
It models the PLAN.md "let winners run" management:

  * a hard stop-loss (`initial_stop`),
  * one or more scaled take-profit rungs (close a fraction of the original
    position at +Nx the initial risk),
  * an optional move of the stop to breakeven once the first rung is hit,
  * an optional ATR trailing stop on the leftover "runner" once every rung has
    been hit.

The module is pure (no pandas, no exchange, no IO) and operates one price-bar at
a time, so it is trivially unit-testable and reusable:

  * Backtest: feed each historical bar's real (high, low, close) + ATR.
  * Live: feed the latest price as high == low == close, carrying `BracketState`
    across polling cycles (persisted on the Position).

Conventions
-----------
All fractions are expressed as a fraction of the ORIGINAL position size, and
`BracketState.remaining` tracks how much of the original is still open (1.0 -> 0).
For a long position, the stop is checked first each bar (worst-case fill), then
take-profit rungs, then the trailing stop is ratcheted up for subsequent bars.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.config import ExitConfig

# Exit reasons (kept as constants so engines/tests share one vocabulary).
REASON_STOP = "stop"
REASON_TRAILING = "trailing_stop"
REASON_TAKE_PROFIT = "take_profit"
REASON_TREND = "trend_exit"
REASON_END = "end_of_data"

_EPS = 1e-9


@dataclass
class BracketState:
    """Mutable state of one open position's exit bracket."""

    entry_price: float
    initial_stop: float
    stop: float            # current stop (may be raised to breakeven / trailed)
    risk_per_unit: float   # entry_price - initial_stop (1R, in price units)
    remaining: float       # fraction of the original position still open (1..0)
    levels_hit: int        # number of take-profit rungs already triggered
    high_water: float      # highest price seen since entry (drives the trail)
    trailing_active: bool

    @classmethod
    def open(cls, entry_price: float, initial_stop: float) -> "BracketState":
        return cls(
            entry_price=entry_price,
            initial_stop=initial_stop,
            stop=initial_stop,
            risk_per_unit=entry_price - initial_stop,
            remaining=1.0,
            levels_hit=0,
            high_water=entry_price,
            trailing_active=False,
        )

    @property
    def closed(self) -> bool:
        return self.remaining <= _EPS


@dataclass
class ExitEvent:
    """A fill produced by the bracket on a single bar."""

    fraction: float  # fraction of the ORIGINAL position closed by this fill
    price: float
    reason: str


def target_price(state: BracketState, reward_mult: float) -> float:
    """Price of a take-profit rung at `reward_mult` x the initial risk."""
    return state.entry_price + reward_mult * state.risk_per_unit


def evaluate_bar(
    state: BracketState,
    high: float,
    low: float,
    atr: float,
    cfg: ExitConfig,
) -> list[ExitEvent]:
    """Advance the bracket by one bar; mutate `state`; return the fills.

    Precedence (long position, conservative):
      1. Stop / trailing stop: if the bar's low pierces the current stop, the
         entire remainder exits at the stop price (worst case wins the bar).
      2. Take-profit rungs: every not-yet-hit rung whose target the bar's high
         reaches fills, in order. After the first fill the stop optionally jumps
         to breakeven; after the last rung the runner starts trailing.
      3. Trailing ratchet: the high-water mark and (if active) the trailing stop
         are updated for FUTURE bars. The freshly raised stop is intentionally
         NOT re-checked against this same bar's low (that would be lookahead).
    """
    events: list[ExitEvent] = []
    if state.closed:
        return events

    # 1) Stop-loss / trailing stop (checked before any profit-taking).
    if low <= state.stop + _EPS:
        reason = REASON_TRAILING if state.trailing_active else REASON_STOP
        events.append(ExitEvent(fraction=state.remaining, price=state.stop, reason=reason))
        state.remaining = 0.0
        return events

    # 2) Scaled take-profit rungs.
    for level in cfg.take_profits[state.levels_hit:]:
        tgt = target_price(state, level.reward_mult)
        if high < tgt - _EPS:
            break  # rungs are ordered; if this one isn't reached, neither are later ones
        fraction = min(level.size_pct, state.remaining)
        if fraction > _EPS:
            events.append(ExitEvent(fraction=fraction, price=tgt, reason=REASON_TAKE_PROFIT))
            state.remaining -= fraction
        state.levels_hit += 1
        if state.levels_hit == 1 and cfg.breakeven_after_first_tp:
            state.stop = max(state.stop, state.entry_price)
        if state.levels_hit >= len(cfg.take_profits) and cfg.trailing_enabled:
            state.trailing_active = True
        if state.closed:
            return events

    # 3) Ratchet the high-water mark and trailing stop for subsequent bars.
    state.high_water = max(state.high_water, high)
    if state.trailing_active and atr > 0:
        state.stop = max(state.stop, state.high_water - cfg.atr_trail_mult * atr)

    return events


def close_remaining(state: BracketState, price: float, reason: str) -> ExitEvent | None:
    """Force-close whatever is left (trend flip, end of data). Mutates `state`."""
    if state.closed:
        return None
    event = ExitEvent(fraction=state.remaining, price=price, reason=reason)
    state.remaining = 0.0
    return event
