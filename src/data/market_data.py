"""Market data access: fetch signal + trend timeframe candles per symbol."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from src.config import Settings
from src.exchange.binance_client import BinanceClient

logger = logging.getLogger("bot.data")


@dataclass
class SymbolData:
    """OHLCV for one symbol across the signal and trend timeframes."""

    symbol: str
    signal: pd.DataFrame
    trend: pd.DataFrame


class MarketData:
    def __init__(self, client: BinanceClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def fetch(self, symbol: str) -> SymbolData:
        tf = self.settings.timeframes
        limit = self.settings.history_candles
        signal_df = self.client.get_klines(symbol, tf.signal, limit)
        trend_df = self.client.get_klines(symbol, tf.trend, limit)
        logger.info(
            "Fetched %s: %d %s candles, %d %s candles",
            symbol, len(signal_df), tf.signal, len(trend_df), tf.trend,
        )
        return SymbolData(symbol=symbol, signal=signal_df, trend=trend_df)

    def fetch_all(self) -> dict[str, SymbolData]:
        return {sym: self.fetch(sym) for sym in self.settings.symbols}
