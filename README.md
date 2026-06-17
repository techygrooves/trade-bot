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
- **Phase 3 ✅** live execution engine: orders (Binance.US/.com), tiny-capital
  sizing, software stop/take-profit, daily-loss kill switch, Telegram alerts,
  crash-safe position persistence.

## Live trading
```bash
python -m src.bot --scan    # print current signals, place NO orders
python -m src.bot --once    # one live decision cycle (safe for cron)
python -m src.bot --loop    # run continuously (needs an always-on host)
```
Config: `exchange_tld` (`us` → Binance.US, `com` → binance.com), `live.sizing_mode`
(`fixed_budget` for small capital), `live.trade_budget_usdt`. Keys/testnet come
from env vars (`BINANCE_API_KEY`, `BINANCE_API_SECRET`, `BINANCE_TESTNET`).

> ⚠️ **Two hard limitations, read before using real money:**
> 1. **Stops are software-managed** — the engine must be running to enforce them.
>    Don't leave an open position with the bot stopped.
> 2. **An ephemeral cloud session is NOT a 24/7 host.** It is reclaimed after
>    inactivity. For real continuous trading, run `--loop` on an always-on host
>    (e.g. a small VPS). Binance.US has no testnet; validate execution on the
>    global testnet (`testnet.binance.vision`, `BINANCE_TESTNET=true`) first.

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
