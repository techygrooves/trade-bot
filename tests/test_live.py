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
    assert abs(t.position.take_profit - 106.0) < 1e-9   # 100 + 1.5*4


def test_take_profit_exit(isolated_state, settings, monkeypatch):
    ex = FakeExchange(price=100.0, usdt=10.0)
    _patch_signal(monkeypatch, _signal(Action.BUY))
    t = LiveTrader(ex, settings, "BTCUSDT")
    t.step()  # enter
    ex.price = 106.0  # hit target
    _patch_signal(monkeypatch, _signal(Action.HOLD, price=106))
    assert t.step() == "exited: take-profit"
    assert len(ex.sells) == 1
    assert t.position is None
    assert t.guard.realized_today > 0


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
