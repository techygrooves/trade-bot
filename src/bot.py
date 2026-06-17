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
from src.strategy.signals import generate_signal


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
    strategy_cfg = config.settings.strategy
    for symbol, data in market.fetch_all().items():
        signal = generate_signal(symbol, data.signal, data.trend, strategy_cfg)
        logger.info(
            "%s -> %s @ %.2f | stop %s | %s",
            symbol,
            signal.action.value,
            signal.price,
            f"{signal.stop_price:.2f}" if signal.stop_price else "n/a",
            "; ".join(signal.reasons) or "all conditions met",
        )

    logger.info("=== Run complete (Phase 1: signals) ===")


if __name__ == "__main__":
    logging.captureWarnings(True)
    main()
