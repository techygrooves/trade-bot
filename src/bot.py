"""Bot entrypoint.

Modes:
  --scan       Print the current signal for each symbol; place no orders.
  --once       Run a single live decision cycle (default). Safe for cron.
  --loop       Run continuously, polling every `poll_seconds`.

The live engine trades ONE symbol (the first configured) at a time, sized for
small capital. Testnet vs live and Binance.US vs .com are controlled by config
and the BINANCE_TESTNET env var. See PLAN.md / README.md.

Run with:  python -m src.bot --scan
"""
from __future__ import annotations

import argparse
import logging
import time

from src.config import load_config
from src.data.market_data import MarketData
from src.exchange.binance_client import BinanceClient
from src.live.trader import LiveTrader
from src.logging_setup import setup_logging
from src.notify.telegram import TelegramNotifier
from src.risk.guard import DailyLossGuard
from src.strategy.signals import generate_signal


def _scan(client: BinanceClient, settings) -> None:
    logger = logging.getLogger("bot")
    market = MarketData(client, settings)
    for symbol, data in market.fetch_all().items():
        sig = generate_signal(symbol, data.signal, data.trend, settings.strategy)
        logger.info(
            "%s -> %s @ %.2f | stop %s | %s",
            symbol, sig.action.value, sig.price,
            f"{sig.stop_price:.2f}" if sig.stop_price else "n/a",
            "; ".join(sig.reasons) or "all conditions met",
        )


def _build_trader(client: BinanceClient, config) -> LiveTrader:
    settings = config.settings
    symbol = settings.symbols[0]
    notifier = TelegramNotifier(
        config.secrets.telegram_bot_token, config.secrets.telegram_chat_id
    )
    guard = DailyLossGuard(
        settings.risk.daily_loss_limit_pct, settings.live.trade_budget_usdt
    )
    return LiveTrader(client, settings, symbol, notifier=notifier, guard=guard)


def main() -> None:
    parser = argparse.ArgumentParser(description="Trend-momentum spot bot")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--scan", action="store_true", help="print signals, no trading")
    group.add_argument("--once", action="store_true", help="one live cycle (default)")
    group.add_argument("--loop", action="store_true", help="run continuously")
    args = parser.parse_args()

    config = load_config()
    logger = setup_logging(config.settings.log_level)

    client = BinanceClient(config.secrets, tld=config.settings.exchange_tld)
    client.ping()

    if args.scan:
        _scan(client, config.settings)
        return

    if not config.secrets.has_binance_keys:
        logger.error("Live trading needs API keys. Set BINANCE_API_KEY/SECRET.")
        return

    trader = _build_trader(client, config)
    logger.info("Live trader ready for %s (testnet=%s)", trader.symbol, client.is_testnet)

    if args.loop:
        interval = config.settings.poll_seconds
        logger.info("Looping every %ss. Ctrl-C to stop.", interval)
        while True:
            try:
                logger.info("status: %s", trader.step())
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.exception("step failed: %s", exc)
            time.sleep(interval)
    else:
        logger.info("status: %s", trader.step())


if __name__ == "__main__":
    logging.captureWarnings(True)
    main()
