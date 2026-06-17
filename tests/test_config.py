"""Smoke tests for config loading."""
from __future__ import annotations

from src.config import DEFAULT_CONFIG_PATH, Settings, load_config


def test_load_config_defaults():
    config = load_config()
    assert config.settings.symbols, "symbols should not be empty"
    assert config.settings.timeframes.signal
    assert config.settings.timeframes.trend
    assert config.settings.risk.risk_per_trade_pct > 0


def test_config_yaml_exists():
    assert DEFAULT_CONFIG_PATH.exists()


def test_settings_validation():
    s = Settings(symbols=["BTCUSDT"], history_candles=100)
    assert s.symbols == ["BTCUSDT"]
    assert s.history_candles == 100
    # Nested defaults still apply.
    assert s.strategy.ema_trend == 200
