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
from src.risk.sizing import plan_entry, round_step_down, sellable_qty, take_profit_price
from src.strategy.signals import Action, generate_signal

logger = logging.getLogger("bot.live")


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
        target = take_profit_price(entry_price, stop, self.settings.live.reward_mult)
        self.position = Position(
            symbol=self.symbol,
            qty=fill["qty"],
            entry_price=entry_price,
            stop_price=stop,
            take_profit=target,
            entry_time=datetime.now(timezone.utc).isoformat(),
        )
        save_position(self.position)
        self._alert(
            f"🟢 BUY {self.symbol}: spent {fill['quote']:.2f} {self.quote} @ "
            f"{entry_price:.4f}, stop {stop:.4f}, target {target:.4f}"
        )
        return "entered"

    def _manage(self, signal) -> str:
        pos = self.position
        assert pos is not None
        price = self.ex.get_price(self.symbol)

        reason = None
        if price <= pos.stop_price:
            reason = "stop-loss"
        elif price >= pos.take_profit:
            reason = "take-profit"
        elif signal.action == Action.EXIT:
            reason = "trend-exit"
        if reason is None:
            return f"holding @ {price:.4f} (stop {pos.stop_price:.4f}, tp {pos.take_profit:.4f})"

        filt = self.filters()
        free_base = self.ex.get_free_balance(self.base)
        qty = sellable_qty(min(free_base, pos.qty), filt["step_size"], filt["min_qty"])
        if qty <= 0:
            return f"exit wanted ({reason}) but qty below min lot; holding"

        fill = self.ex.market_sell(self.symbol, round_step_down(qty, filt["step_size"]))
        pnl = fill["quote"] - pos.qty * pos.entry_price
        self.guard.record(pnl)
        clear_position(self.symbol)
        self.position = None
        emoji = "🟩" if pnl >= 0 else "🟥"
        self._alert(
            f"{emoji} SELL {self.symbol} ({reason}) @ {fill['price']:.4f} | "
            f"PnL {pnl:+.2f} {self.quote}"
        )
        return f"exited: {reason}"
