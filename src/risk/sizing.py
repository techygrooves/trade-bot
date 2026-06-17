"""Pure position-sizing and exchange-filter rounding helpers.

Kept free of any exchange/network calls so the math is fully unit-tested.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from src.config import LiveConfig, RiskConfig


def round_step_down(qty: float, step: float) -> float:
    """Round a quantity DOWN to the exchange lot step size."""
    if step <= 0:
        return qty
    return math.floor(qty / step) * step


def round_tick(price: float, tick: float) -> float:
    """Round a price to the exchange tick size."""
    if tick <= 0:
        return price
    return math.floor(price / tick) * tick


@dataclass
class EntryPlan:
    ok: bool
    quote_to_spend: float
    reason: str = ""


def plan_entry(
    free_quote: float,
    entry_price: float,
    stop_price: float,
    live_cfg: LiveConfig,
    risk_cfg: RiskConfig,
    min_notional: float,
) -> EntryPlan:
    """Decide how much quote (USDT) to spend on an entry.

    fixed_budget: spend `trade_budget_usdt` (capped by free balance).
    risk_pct:     spend so that the stop distance risks `risk_per_trade_pct`
                  of free balance, capped by free balance.
    Rejects the trade if the resulting spend can't meet the exchange minimum.
    """
    if entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price:
        return EntryPlan(False, 0.0, "invalid entry/stop prices")

    if live_cfg.sizing_mode == "risk_pct":
        risk_amount = free_quote * (risk_cfg.risk_per_trade_pct / 100.0)
        stop_frac = (entry_price - stop_price) / entry_price
        spend = risk_amount / stop_frac if stop_frac > 0 else 0.0
    else:  # fixed_budget
        spend = live_cfg.trade_budget_usdt

    spend = min(spend, free_quote)

    if spend <= 0:
        return EntryPlan(False, 0.0, "no free balance")
    floor = max(min_notional, 0.0)
    if spend < floor:
        return EntryPlan(
            False, spend,
            f"spend {spend:.2f} below exchange minimum {floor:.2f}",
        )
    return EntryPlan(True, round(spend, 2))


def take_profit_price(entry_price: float, stop_price: float, reward_mult: float) -> float:
    """Take-profit at `reward_mult` times the stop distance above entry."""
    return entry_price + reward_mult * (entry_price - stop_price)


def sellable_qty(free_base: float, step: float, min_qty: float) -> float:
    """Largest step-aligned quantity we can sell; 0 if below min lot."""
    qty = round_step_down(free_base, step)
    return qty if qty >= min_qty else 0.0
