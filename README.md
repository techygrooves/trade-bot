# trade-bot

Automated **Binance spot** trend-momentum trading bot, focused on a
risk-controlled, high-probability edge. See [`PLAN.md`](./PLAN.md) for the full
strategy, architecture, and roadmap.

> ⚠️ Trading crypto carries real risk of loss. This bot defaults to **Binance
> Testnet**. Do not point it at live funds until you have backtested and
> paper-traded it, and even then use only capital you can afford to lose. For
> live keys: enable spot trading, **disable withdrawals**.

## Status
**Phase 0 — scaffold.** Config/env loading, logging, Binance testnet
connectivity, and OHLCV fetching are in place. Strategy, risk, execution, and
the live loop come in later phases (see `PLAN.md`).

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
