"""Tests for the live trading state machine against a fake exchange."""
from __future__ import annotations

import pandas as pd
import pytest

import src.live.state as state_mod
import src.live.trader as trader_mod
from src.config import Settings
from src.live.trader import LiveTrader
from src.strategy.signals import Action, SignalResult


class FakeExchange:
    def __init__(self, price=100.0, usdt=10.0):
        self.price = price
        self.balances = {"USDT": usdt, "BTC": 0.0}
        self.flt = {
            "step_size": 0.0001, "min_qty": 0.0001,
            "tick_size": 0.01, "min_notional": 5.0,
        }
        self.buys: list = []
        self.sells: list = []

    def get_klines(self, symbol, interval, limit):
        return pd.DataFrame()  # unused: signal is injected via monkeypatch

    def get_price(self, symbol):
        return self.price

    def get_free_balance(self, asset):
        return self.balances.get(asset, 0.0)

    def get_symbol_filters(self, symbol):
        return self.flt

    def market_buy_quote(self, symbol, quote_qty):
        qty = quote_qty / self.price
        self.balances["USDT"] -= quote_qty
        self.balances["BTC"] += qty
        self.buys.append((quote_qty, qty))
        return {"price": self.price, "qty": qty, "quote": quote_qty, "raw": {}}

    def market_sell(self, symbol, qty):
        quote = qty * self.price
        self.balances["BTC"] -= qty
        self.balances["USDT"] += quote
        self.sells.append((qty, quote))
        return {"price": self.price, "qty": qty, "quote": quote, "raw": {}}


def _signal(action, price=100.0, atr=2.0, stop=96.0):
    return SignalResult(
        action=action, symbol="BTCUSDT", price=price, atr=atr,
        stop_price=stop, timestamp=pd.Timestamp("2024-01-01", tz="UTC"), reasons=[],
    )


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "STATE_DIR", tmp_path / "state")


@pytest.fixture
def settings():
    return Settings(symbols=["BTCUSDT"])


def _patch_signal(monkeypatch, result):
    monkeypatch.setattr(trader_mod, "generate_signal", lambda *a, **k: result)


def test_enters_on_buy(isolated_state, settings, monkeypatch):
    ex = FakeExchange(price=100.0, usdt=10.0)
    _patch_signal(monkeypatch, _signal(Action.BUY, price=100, atr=2, stop=96))
    t = LiveTrader(ex, settings, "BTCUSDT")
    assert t.step() == "entered"
    assert len(ex.buys) == 1 and ex.buys[0][0] == 10.0  # spent 10 USDT
    assert t.position is not None
    assert abs(t.position.stop_price - 96.0) < 1e-9     # 100 - 2*2
    assert abs(t.position.initial_stop - 96.0) < 1e-9
    assert abs(t.position.risk_per_unit - 4.0) < 1e-9
    assert abs(t.position.initial_qty - t.position.qty) < 1e-9  # full size at entry
    assert t.position.levels_hit == 0 and not t.position.trailing_active


def test_take_profit_exit(isolated_state, settings, monkeypatch):
    # Tiny (10 USDT) account: a 1/3 scale-out is below the min lot/notional, so
    # the engine gracefully banks the whole position at the first take-profit.
    ex = FakeExchange(price=100.0, usdt=10.0)
    _patch_signal(monkeypatch, _signal(Action.BUY))
    t = LiveTrader(ex, settings, "BTCUSDT")
    t.step()  # enter
    ex.price = 106.0  # hit first rung (+1.5R)
    _patch_signal(monkeypatch, _signal(Action.HOLD, price=106))
    assert t.step() == "exited: take-profit"
    assert len(ex.sells) == 1
    assert t.position is None
    assert t.guard.realized_today > 0


def test_scaled_take_profit_partial(isolated_state, monkeypatch):
    # A larger account can actually scale out: 1/3 at +1.5R, stop -> breakeven,
    # the runner stays open.
    from src.config import LiveConfig, Settings

    settings = Settings(symbols=["BTCUSDT"], live=LiveConfig(trade_budget_usdt=1000.0))
    ex = FakeExchange(price=100.0, usdt=2000.0)
    _patch_signal(monkeypatch, _signal(Action.BUY, price=100, atr=2, stop=96))
    t = LiveTrader(ex, settings, "BTCUSDT")
    t.step()  # enter ~10 BTC
    entry_qty = t.position.initial_qty
    ex.price = 106.0  # +1.5R -> first rung only
    _patch_signal(monkeypatch, _signal(Action.HOLD, price=106))
    status = t.step()
    assert status == "scaled out: rung 1"
    assert t.position is not None
    assert t.position.levels_hit == 1
    assert abs(t.position.stop_price - 100.0) < 1e-9        # moved to breakeven
    assert t.position.qty < entry_qty                       # partially sold
    assert len(ex.sells) == 1
    assert t.guard.realized_today > 0


def test_trailing_stop_after_full_scale_out(isolated_state, monkeypatch):
    from src.config import LiveConfig, Settings

    settings = Settings(symbols=["BTCUSDT"], live=LiveConfig(trade_budget_usdt=1000.0))
    ex = FakeExchange(price=100.0, usdt=2000.0)
    _patch_signal(monkeypatch, _signal(Action.BUY, price=100, atr=2, stop=96))
    t = LiveTrader(ex, settings, "BTCUSDT")
    t.step()
    ex.price = 113.0  # clears both rungs -> trailing arms (stop 113-2*2=109)
    _patch_signal(monkeypatch, _signal(Action.HOLD, price=113, atr=2))
    t.step()
    assert t.position is not None and t.position.trailing_active
    assert abs(t.position.stop_price - 109.0) < 1e-9
    ex.price = 108.0  # below trailing stop -> exit the runner
    _patch_signal(monkeypatch, _signal(Action.HOLD, price=108, atr=2))
    assert t.step() == "exited: trailing-stop"
    assert t.position is None


def test_stop_loss_exit(isolated_state, settings, monkeypatch):
    ex = FakeExchange(price=100.0, usdt=10.0)
    _patch_signal(monkeypatch, _signal(Action.BUY))
    t = LiveTrader(ex, settings, "BTCUSDT")
    t.step()
    ex.price = 95.0  # below stop 96
    _patch_signal(monkeypatch, _signal(Action.HOLD, price=95))
    assert t.step() == "exited: stop-loss"
    assert t.guard.realized_today < 0


def test_trend_exit(isolated_state, settings, monkeypatch):
    ex = FakeExchange(price=100.0, usdt=10.0)
    _patch_signal(monkeypatch, _signal(Action.BUY))
    t = LiveTrader(ex, settings, "BTCUSDT")
    t.step()
    ex.price = 101.0  # between stop and target
    _patch_signal(monkeypatch, _signal(Action.EXIT, price=101))
    assert t.step() == "exited: trend-exit"


def test_kill_switch_blocks_entry(isolated_state, settings, monkeypatch):
    ex = FakeExchange(price=100.0, usdt=10.0)
    _patch_signal(monkeypatch, _signal(Action.BUY))
    t = LiveTrader(ex, settings, "BTCUSDT")
    t.guard.record(-1.0)  # 1 USDT loss > 5% of 10 USDT reference
    assert t.step() == "halted: daily loss limit reached"
    assert len(ex.buys) == 0


def test_entry_skipped_when_below_min_notional(isolated_state, settings, monkeypatch):
    ex = FakeExchange(price=100.0, usdt=3.0)  # only 3 USDT, min notional 5
    _patch_signal(monkeypatch, _signal(Action.BUY))
    t = LiveTrader(ex, settings, "BTCUSDT")
    status = t.step()
    assert status.startswith("entry skipped")
    assert len(ex.buys) == 0


def test_position_persists_across_restart(isolated_state, settings, monkeypatch):
    ex = FakeExchange(price=100.0, usdt=10.0)
    _patch_signal(monkeypatch, _signal(Action.BUY))
    LiveTrader(ex, settings, "BTCUSDT").step()  # enters and saves state
    # New trader instance (simulating a restart) reloads the open position.
    t2 = LiveTrader(ex, settings, "BTCUSDT")
    assert t2.position is not None
    assert abs(t2.position.entry_price - 100.0) < 1e-9
