"""Thin wrapper around python-binance.

Handles testnet vs live selection, connectivity checks, and OHLCV fetching.
Higher layers (strategy, execution) talk to this — never to python-binance
directly — so we can swap or mock the exchange easily.
"""
from __future__ import annotations

import logging

import pandas as pd
from binance.client import Client

from src.config import Secrets

logger = logging.getLogger("bot.exchange")

# Columns returned by Binance klines endpoint.
_KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_base", "taker_quote", "ignore",
]


class BinanceClient:
    def __init__(self, secrets: Secrets) -> None:
        self._testnet = secrets.binance_testnet
        self.client = Client(
            api_key=secrets.binance_api_key or None,
            api_secret=secrets.binance_api_secret or None,
            testnet=secrets.binance_testnet,
        )
        mode = "TESTNET" if secrets.binance_testnet else "LIVE"
        logger.info("Binance client initialized in %s mode", mode)

    @property
    def is_testnet(self) -> bool:
        return self._testnet

    def ping(self) -> bool:
        """Verify connectivity and (if keys present) authentication."""
        self.client.ping()
        server_time = self.client.get_server_time()
        logger.info("Connectivity OK. Server time: %s", server_time.get("serverTime"))
        return True

    def get_account_balances(self) -> dict[str, float]:
        """Return non-zero free balances. Requires valid API keys."""
        account = self.client.get_account()
        balances = {
            b["asset"]: float(b["free"])
            for b in account["balances"]
            if float(b["free"]) > 0
        }
        return balances

    def get_klines(self, symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
        """Fetch OHLCV candles as a typed, time-indexed DataFrame."""
        raw = self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(raw, columns=_KLINE_COLUMNS)

        numeric = ["open", "high", "low", "close", "volume", "quote_volume"]
        df[numeric] = df[numeric].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        df = df.set_index("open_time")

        return df[["open", "high", "low", "close", "volume", "quote_volume", "close_time"]]
