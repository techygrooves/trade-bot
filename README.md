# trade-bot

Automated **Binance spot** trend-momentum trading bot, focused on a
risk-controlled, high-probability edge. See [`PLAN.md`](./PLAN.md) for the full
strategy, architecture, and roadmap.

> ⚠️ Trading crypto carries real risk of loss. This bot defaults to **Binance
> Testnet**. Do not point it at live funds until you have backtested and
> paper-traded it, and even then use only capital you can afford to lose. For
> live keys: enable spot trading, **disable withdrawals**.

## Status
- **Phase 0 ✅** scaffold: config/env loading, logging, Binance client, OHLCV fetch.
- **Phase 1 ✅** indicators (EMA/RSI/MACD/ATR/ADX) + trend-momentum signal engine.
- **Phase 2 ✅** event-driven backtester with risk-based sizing, fees, and metrics.

Risk/execution, the live trading loop, and Telegram alerts come next (see `PLAN.md`).

## Backtesting
```bash
# From Binance public history (needs data.binance.vision on the egress allowlist):
python -m src.backtest --symbol BTCUSDT --start 2023-01 --end 2024-12

# From a local CSV of signal-interval candles:
python -m src.backtest --csv data/BTCUSDT-1h.csv --symbol BTCUSDT
```
The backtester reports win rate, profit factor, expectancy (R), total return, and
max drawdown. It avoids lookahead (decisions use closed candles; fills at the next
bar's open) and is spot-only (no leverage; equity can't go negative).

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env (testnet keys to start)
```

Get Binance **Spot Testnet** keys at https://testnet.binance.vision and put them
in `.env`. `BINANCE_TESTNET=true` keeps you on the testnet.

## Run (Phase 0)
```bash
python -m src.bot
```
This connects to Binance, optionally checks your account, and fetches recent
candles for the configured symbols — proving the data pipeline end to end.
Public market data works even without API keys.

## Test
```bash
pytest
```

## Configuration
- **Secrets** (API keys, testnet flag): `.env` — never committed.
- **Settings** (symbols, timeframes, strategy/risk params): `config/config.yaml`.

## Layout
```
src/
├── config.py            # env + yaml config (pydantic-validated)
├── logging_setup.py     # console + rotating file logs
├── exchange/            # Binance client wrapper
├── data/                # OHLCV fetching
└── bot.py               # Phase 0 entrypoint
```
