"""Live trading engine: one symbol, one position at a time.

A single `step()` runs one decision cycle, so it is safe to call from a polling
loop, a scheduler, or a cron job. It is exchange-agnostic (talks to the Exchange
protocol below), which keeps it fully testable against a fake exchange.

Risk controls baked in:
  * software stop-loss and take-profit, checked every step
  * higher-timeframe trend-flip exit
  * daily-loss kill switch (no new entries once tripped)
  * crash-safe position persistence (resumes holdings after a restart)

Limitation (documented honestly): stops are software-managed, so the engine must
be running to enforce them. Do not leave an open position unattended.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol

import pandas as pd

from src.config import Settings
from src.live.state import Position, clear_position, load_position, save_position
from src.notify.telegram import TelegramNotifier
from src.risk.guard import DailyLossGuard
from src.risk.sizing import plan_entry, round_step_down, sellable_qty
from src.strategy.exit_policy import (
    REASON_STOP,
    REASON_TAKE_PROFIT,
    REASON_TRAILING,
    REASON_TREND,
    BracketState,
    close_remaining,
    evaluate_bar,
)
from src.strategy.signals import Action, generate_signal

logger = logging.getLogger("bot.live")

# Human-readable labels for the shared exit reasons (used in status + alerts).
_REASON_LABEL = {
    REASON_STOP: "stop-loss",
    REASON_TRAILING: "trailing-stop",
    REASON_TAKE_PROFIT: "take-profit",
    REASON_TREND: "trend-exit",
}


class Exchange(Protocol):
    def get_klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame: ...
    def get_price(self, symbol: str) -> float: ...
    def get_free_balance(self, asset: str) -> float: ...
    def get_symbol_filters(self, symbol: str) -> dict[str, float]: ...
    def market_buy_quote(self, symbol: str, quote_qty: float) -> dict: ...
    def market_sell(self, symbol: str, qty: float) -> dict: ...


class LiveTrader:
    def __init__(
        self,
        exchange: Exchange,
        settings: Settings,
        symbol: str,
        notifier: TelegramNotifier | None = None,
        guard: DailyLossGuard | None = None,
    ) -> None:
        self.ex = exchange
        self.settings = settings
        self.symbol = symbol
        self.quote = settings.live.quote_asset
        self.base = symbol[: -len(self.quote)]
        self.notifier = notifier or TelegramNotifier("", "")
        self.guard = guard or DailyLossGuard(
            settings.risk.daily_loss_limit_pct, settings.live.trade_budget_usdt
        )
        self._filters: dict[str, float] | None = None
        self.position: Position | None = load_position(symbol)

    def _alert(self, message: str) -> None:
        logger.info(message)
        self.notifier.send(message)

    def filters(self) -> dict[str, float]:
        if self._filters is None:
            self._filters = self.ex.get_symbol_filters(self.symbol)
        return self._filters

    def _signal(self):
        tf = self.settings.timeframes
        n = self.settings.history_candles
        sig = self.ex.get_klines(self.symbol, tf.signal, n)
        trend = self.ex.get_klines(self.symbol, tf.trend, n)
        return generate_signal(self.symbol, sig, trend, self.settings.strategy)

    def step(self) -> str:
        """Run one decision cycle. Returns a short status string."""
        signal = self._signal()

        if self.position is not None:
            return self._manage(signal)

        if self.guard.trading_halted():
            return "halted: daily loss limit reached"
        if signal.action != Action.BUY:
            return f"flat: {signal.action.value}"
        return self._enter(signal)

    def _enter(self, signal) -> str:
        if signal.stop_price is None:
            return "flat: no valid stop"
        filt = self.filters()
        free_quote = self.ex.get_free_balance(self.quote)
        plan = plan_entry(
            free_quote, signal.price, signal.stop_price,
            self.settings.live, self.settings.risk, filt["min_notional"],
        )
        if not plan.ok:
            return f"entry skipped: {plan.reason}"

        fill = self.ex.market_buy_quote(self.symbol, plan.quote_to_spend)
        entry_price = fill["price"]
        stop = entry_price - self.settings.strategy.atr_stop_mult * signal.atr
        self.position = Position(
            symbol=self.symbol,
            qty=fill["qty"],
            initial_qty=fill["qty"],
            entry_price=entry_price,
            initial_stop=stop,
            stop_price=stop,
            risk_per_unit=entry_price - stop,
            high_water=entry_price,
            levels_hit=0,
            trailing_active=False,
            entry_time=datetime.now(timezone.utc).isoformat(),
        )
        save_position(self.position)
        first_target = self._first_target(entry_price, stop)
        self._alert(
            f"🟢 BUY {self.symbol}: spent {fill['quote']:.2f} {self.quote} @ "
            f"{entry_price:.4f}, stop {stop:.4f}, first target {first_target:.4f}"
        )
        return "entered"

    def _first_target(self, entry_price: float, stop: float) -> float:
        """Price of the first take-profit rung (for the entry alert)."""
        exits = self.settings.exits
        reward = (
            exits.take_profits[0].reward_mult
            if exits.take_profits
            else self.settings.live.reward_mult
        )
        return entry_price + reward * (entry_price - stop)

    def _bracket_state(self, pos: Position) -> BracketState:
        return BracketState(
            entry_price=pos.entry_price,
            initial_stop=pos.initial_stop,
            stop=pos.stop_price,
            risk_per_unit=pos.risk_per_unit,
            remaining=pos.qty / pos.initial_qty if pos.initial_qty > 0 else 1.0,
            levels_hit=pos.levels_hit,
            high_water=pos.high_water,
            trailing_active=pos.trailing_active,
        )

    def _sync_position(self, pos: Position, state: BracketState) -> None:
        """Persist bracket progress (trailed stop, scale-out, high-water)."""
        pos.stop_price = state.stop
        pos.levels_hit = state.levels_hit
        pos.high_water = state.high_water
        pos.trailing_active = state.trailing_active
        save_position(pos)

    def _liquidate(self, pos: Position, price: float, reason: str) -> str:
        """Sell the entire remaining position at market and close it out."""
        filt = self.filters()
        free_base = self.ex.get_free_balance(self.base)
        qty = sellable_qty(min(free_base, pos.qty), filt["step_size"], filt["min_qty"])
        if qty <= 0:
            # Dust below the minimum lot: nothing tradeable left, treat as closed.
            clear_position(self.symbol)
            self.position = None
            logger.info("%s remainder below min lot; closing out as dust", self.symbol)
            return f"exited: {_REASON_LABEL.get(reason, reason)}"
        fill = self.ex.market_sell(self.symbol, round_step_down(qty, filt["step_size"]))
        pnl = fill["quote"] - fill["qty"] * pos.entry_price
        self.guard.record(pnl)
        clear_position(self.symbol)
        self.position = None
        label = _REASON_LABEL.get(reason, reason)
        emoji = "🟩" if pnl >= 0 else "🟥"
        self._alert(
            f"{emoji} SELL {self.symbol} ({label}) @ {fill['price']:.4f} | "
            f"PnL {pnl:+.2f} {self.quote}"
        )
        return f"exited: {label}"

    def _manage(self, signal) -> str:
        pos = self.position
        assert pos is not None
        price = self.ex.get_price(self.symbol)
        exits = self.settings.exits

        state = self._bracket_state(pos)
        # Treat the polled price as a one-tick bar (high == low == close).
        events = evaluate_bar(state, price, price, signal.atr, exits)
        # A higher-timeframe trend flip exits whatever is left.
        if not state.closed and signal.action == Action.EXIT:
            ev = close_remaining(state, price, REASON_TREND)
            if ev is not None:
                events.append(ev)

        if not events:
            self._sync_position(pos, state)  # persist any trailing-stop ratchet
            return (
                f"holding @ {price:.4f} (stop {pos.stop_price:.4f}, "
                f"rungs {pos.levels_hit}/{len(exits.take_profits)})"
            )

        terminal_reason = events[-1].reason
        if state.closed:
            # Final fill (stop / trailing / trend, or take-profits that fully exit).
            return self._liquidate(pos, events[-1].price, terminal_reason)

        # Partial scale-out: only take-profit rungs fired, a runner remains.
        filt = self.filters()
        free_base = self.ex.get_free_balance(self.base)
        want_fraction = sum(ev.fraction for ev in events)
        want_qty = want_fraction * pos.initial_qty
        sell_qty = round_step_down(
            min(want_qty, free_base, pos.qty), filt["step_size"]
        )
        if sell_qty < filt["min_qty"] or sell_qty * price < filt["min_notional"]:
            # Too small to scale out cleanly -> bank the whole position now.
            return self._liquidate(pos, events[-1].price, REASON_TAKE_PROFIT)

        fill = self.ex.market_sell(self.symbol, sell_qty)
        pnl = fill["quote"] - fill["qty"] * pos.entry_price
        self.guard.record(pnl)
        pos.qty -= fill["qty"]
        self._sync_position(pos, state)
        emoji = "🟩" if pnl >= 0 else "🟥"
        self._alert(
            f"{emoji} SCALE-OUT {self.symbol} (rung {pos.levels_hit}) @ "
            f"{fill['price']:.4f} | PnL {pnl:+.2f} {self.quote} | "
            f"stop -> {pos.stop_price:.4f}"
        )
        return f"scaled out: rung {pos.levels_hit}"
