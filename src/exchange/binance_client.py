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


def _avg_fill(order: dict) -> tuple[float, float, float]:
    """Average fill price, filled base qty, and quote spent from an order resp."""
    fills = order.get("fills") or []
    qty = float(order.get("executedQty", 0.0))
    if fills:
        quote = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        base = sum(float(f["qty"]) for f in fills)
        price = quote / base if base else 0.0
        return price, base, quote
    quote = float(order.get("cummulativeQuoteQty", 0.0))
    price = quote / qty if qty else 0.0
    return price, qty, quote

# Columns returned by Binance klines endpoint.
_KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_base", "taker_quote", "ignore",
]


class BinanceClient:
    def __init__(self, secrets: Secrets, tld: str = "com") -> None:
        self._testnet = secrets.binance_testnet
        # Testnet (testnet.binance.vision) is global; tld only applies to live.
        kwargs: dict = {
            "api_key": secrets.binance_api_key or None,
            "api_secret": secrets.binance_api_secret or None,
            "testnet": secrets.binance_testnet,
        }
        if not secrets.binance_testnet:
            kwargs["tld"] = tld
        self.client = Client(**kwargs)
        venue = "TESTNET" if secrets.binance_testnet else f"LIVE (binance.{tld})"
        logger.info("Binance client initialized: %s", venue)

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

    # --- Live trading helpers (used by the execution engine) ---

    def get_price(self, symbol: str) -> float:
        return float(self.client.get_symbol_ticker(symbol=symbol)["price"])

    def get_free_balance(self, asset: str) -> float:
        bal = self.client.get_asset_balance(asset=asset)
        return float(bal["free"]) if bal else 0.0

    def get_symbol_filters(self, symbol: str) -> dict[str, float]:
        """Return the LOT_SIZE / PRICE_FILTER / MIN_NOTIONAL constraints."""
        info = self.client.get_symbol_info(symbol)
        out = {"step_size": 0.0, "min_qty": 0.0, "tick_size": 0.0, "min_notional": 0.0}
        for f in info["filters"]:
            ftype = f["filterType"]
            if ftype == "LOT_SIZE":
                out["step_size"] = float(f["stepSize"])
                out["min_qty"] = float(f["minQty"])
            elif ftype == "PRICE_FILTER":
                out["tick_size"] = float(f["tickSize"])
            elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                out["min_notional"] = float(f.get("minNotional", 0.0))
        return out

    def market_buy_quote(self, symbol: str, quote_qty: float) -> dict:
        """Market BUY spending an exact amount of the quote asset (e.g. USDT)."""
        order = self.client.order_market_buy(
            symbol=symbol, quoteOrderQty=round(quote_qty, 2)
        )
        price, base, quote = _avg_fill(order)
        logger.info("BUY %s: spent %.2f, got %.8f @ %.4f", symbol, quote, base, price)
        return {"price": price, "qty": base, "quote": quote, "raw": order}

    def market_sell(self, symbol: str, qty: float) -> dict:
        """Market SELL a base-asset quantity (caller rounds to step size)."""
        order = self.client.order_market_sell(symbol=symbol, quantity=qty)
        price, base, quote = _avg_fill(order)
        logger.info("SELL %s: sold %.8f @ %.4f for %.2f", symbol, base, price, quote)
        return {"price": price, "qty": base, "quote": quote, "raw": order}
