"""Phase 0 entrypoint.

Loads config, sets up logging, connects to Binance (testnet by default), and
fetches OHLCV for the configured symbols to prove the data pipeline works.

Run with:  python -m src.bot
Strategy, risk, execution, and the live trading loop are added in later phases.
"""
from __future__ import annotations

import logging

from src.config import load_config
from src.data.market_data import MarketData
from src.exchange.binance_client import BinanceClient
from src.logging_setup import setup_logging


def main() -> None:
    config = load_config()
    logger = setup_logging(config.settings.log_level)
    logger.info("=== Trade bot starting (Phase 0 scaffold) ===")

    client = BinanceClient(config.secrets)
    client.ping()

    if config.secrets.has_binance_keys:
        try:
            balances = client.get_account_balances()
            logger.info("Account balances (non-zero): %s", balances)
        except Exception as exc:  # noqa: BLE001 - log and continue in scaffold
            logger.warning("Could not fetch balances (check API keys): %s", exc)
    else:
        logger.warning(
            "No Binance API keys set; skipping account check. "
            "Public market data still works. Copy .env.example to .env to add keys."
        )

    market = MarketData(client, config.settings)
    for symbol, data in market.fetch_all().items():
        last = data.signal.iloc[-1]
        logger.info(
            "%s last %s close=%.2f volume=%.2f",
            symbol, config.settings.timeframes.signal, last["close"], last["volume"],
        )

    logger.info("=== Phase 0 scaffold run complete ===")


if __name__ == "__main__":
    logging.captureWarnings(True)
    main()
