"""Configuration loading: secrets from .env, settings from config/config.yaml.

Centralizes all config so the rest of the bot never reads env vars or files
directly. Validated with pydantic so a misconfiguration fails loudly at startup
instead of mid-trade.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


class Secrets(BaseSettings):
    """Sensitive values, loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    binance_api_key: str = Field(default="", alias="BINANCE_API_KEY")
    binance_api_secret: str = Field(default="", alias="BINANCE_API_SECRET")
    binance_testnet: bool = Field(default=True, alias="BINANCE_TESTNET")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    @property
    def has_binance_keys(self) -> bool:
        return bool(self.binance_api_key and self.binance_api_secret)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


class Timeframes(BaseModel):
    signal: str = "1h"
    trend: str = "4h"


class StrategyConfig(BaseModel):
    ema_fast: int = 20
    ema_slow: int = 50
    ema_trend: int = 200
    rsi_period: int = 14
    rsi_lower: float = 40
    rsi_upper: float = 60
    adx_period: int = 14
    adx_min: float = 20
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    vol_ma_period: int = 20


class RiskConfig(BaseModel):
    risk_per_trade_pct: float = 1.0
    max_open_positions: int = 3
    daily_loss_limit_pct: float = 5.0
    taker_fee_pct: float = 0.1


class TakeProfitLevel(BaseModel):
    """One scaled take-profit rung.

    `reward_mult`: target distance above entry as a multiple of the stop distance
                   (the initial risk, 1R). e.g. 1.5 -> take profit at +1.5R.
    `size_pct`:    fraction of the ORIGINAL position to close at this rung (0..1).
    """

    reward_mult: float
    size_pct: float


class ExitConfig(BaseModel):
    """Exit/trade-management policy, shared by the backtester and live trader.

    Implements the PLAN.md "let winners run" management:
      * scale out at one or more take-profit rungs (`take_profits`),
      * move the stop to breakeven once the first rung is hit
        (`breakeven_after_first_tp`),
      * trail the remaining runner by `atr_trail_mult` ATRs once every configured
        rung has been hit (`trailing_enabled`).

    The fractions in `take_profits` should sum to <= 1.0; whatever is left over is
    the "runner" that rides the trailing stop / higher-timeframe trend exit.
    """

    take_profits: list[TakeProfitLevel] = Field(
        default_factory=lambda: [
            TakeProfitLevel(reward_mult=1.5, size_pct=0.34),
            TakeProfitLevel(reward_mult=3.0, size_pct=0.33),
        ]
    )
    breakeven_after_first_tp: bool = True
    trailing_enabled: bool = True
    atr_trail_mult: float = 2.0  # trailing-stop distance, in ATRs, for the runner


class LiveConfig(BaseModel):
    """Live trading parameters.

    `sizing_mode`:
      - "fixed_budget": spend `trade_budget_usdt` per trade (for tiny capital,
        where the 1%-risk model can't meet the exchange minimum order size).
      - "risk_pct": size from RiskConfig.risk_per_trade_pct and the stop distance.
    """

    sizing_mode: str = "fixed_budget"
    trade_budget_usdt: float = 10.0
    quote_asset: str = "USDT"
    reward_mult: float = 1.5      # legacy single-target fallback (see ExitConfig)


class Settings(BaseModel):
    """Non-secret runtime settings, loaded from config.yaml."""

    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    timeframes: Timeframes = Field(default_factory=Timeframes)
    history_candles: int = 500
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    live: LiveConfig = Field(default_factory=LiveConfig)
    exits: ExitConfig = Field(default_factory=ExitConfig)
    exchange_tld: str = "com"     # "com" -> api.binance.com, "us" -> api.binance.us
    poll_seconds: int = 60
    log_level: str = "INFO"


class Config(BaseModel):
    secrets: Secrets
    settings: Settings


def load_config(config_path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """Load and validate the full configuration."""
    path = Path(config_path)
    raw: dict = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    return Config(secrets=Secrets(), settings=Settings(**raw))
