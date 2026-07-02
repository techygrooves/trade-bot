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
- **Phase A ✅** honest backtester: pluggable exit schemes (trend / fixed_tp /
  scaled), gap-aware fills, slippage, Sharpe + per-exit-reason metrics.
- **Phase B 🚧** validation harness built (multi-symbol scheme comparison,
  train/validation parameter sweep, expectancy/drawdown decision gate); the
  real 2021–2025 data run is pending network access to `data.binance.vision`
  — see "Phase B: choosing the exit scheme" below.

## Live trading
```bash
python -m src.bot --scan    # print current signals, place NO orders
python -m src.bot --once    # one live decision cycle (safe for cron)
python -m src.bot --loop    # run continuously (needs an always-on host)
```
Config: `exchange_tld` (`com` → binance.com global [default], `us` → Binance.US),
`live.sizing_mode` (`fixed_budget` for small capital), `live.trade_budget_usdt`.
Keys/testnet come from env vars (`BINANCE_API_KEY`, `BINANCE_API_SECRET`,
`BINANCE_TESTNET`).

**Validate first on the testnet.** binance.com has a full spot testnet at
`testnet.binance.vision` — get keys there, set `BINANCE_TESTNET=true`, and run a
full buy/sell cycle before pointing at live funds.

> ⚠️ **Two hard limitations, read before using real money:**
> 1. **Stops are software-managed** — the engine must be running to enforce them.
>    Don't leave an open position with the bot stopped.
> 2. **An ephemeral cloud session is NOT a 24/7 host.** It is reclaimed after
>    inactivity. For real continuous trading, run `--loop` on an always-on host
>    (e.g. a small VPS).
>
> Note: binance.com geo-restricts some regions/datacenter IPs. If you hit a
> "restricted location" error, run from an allowed host/region.

## Backtesting
```bash
# From Binance public history (needs data.binance.vision on the egress allowlist):
python -m src.backtest --symbol BTCUSDT --start 2023-01 --end 2024-12

# From a local CSV of signal-interval candles:
python -m src.backtest --csv data/BTCUSDT-1h.csv --symbol BTCUSDT

# Compare exit schemes on the same data:
python -m src.backtest --csv data/BTCUSDT-1h.csv --exit-scheme scaled
```
Three exit schemes are supported (`exits.scheme` in config, `--exit-scheme` to
override): `trend` (stop or higher-TF trend flip only), `fixed_tp` (full exit
at `reward_mult`× the stop distance — matches the live engine, so this scheme's
backtests are evidence for live behavior), and `scaled` (partial take-profits
at 1.5R/3R, ATR-trailing stop on the remainder).

## Phase B: choosing the exit scheme

One command runs the whole validation experiment — 2021–2025 multi-symbol
backtests of all three exit schemes, a small parameter sweep picked on a train
window and scored once on a held-out validation window, and a decision gate
that selects by pooled expectancy, profit factor and worst drawdown (never win
rate):

```bash
# Downloads + caches candles under data/ on first run
# (requires data.binance.vision on the network egress allowlist),
# then writes reports/phase_b/report.md + CSVs:
python -m src.backtest.phase_b

# Later runs work fully offline from the cache:
python -m src.backtest.phase_b --offline

# Just prefetch candles (e.g. on a machine with network) without backtesting:
python -m src.backtest.phase_b --fetch-only
```

Defaults: BTC/ETH/BNB/SOL, 1h signal / 4h trend candles fetched from 2020-10
(warm-up) through 2025-12, train 2021-01-01..2023-12-31, validation
2024-01-01..2025-12-31. All windows/symbols/gates are flags — see
`python -m src.backtest.phase_b --help`. The winning scheme + parameters get
recorded in `NEXT_STEPS.md` and implemented live in Phase C.

The backtester reports win rate, profit factor, expectancy (R), total return,
max drawdown, annualized Sharpe, average holding time, and a per-exit-reason
breakdown. Fills are kept honest: decisions use closed candles and execute at
the next bar's open; market fills (entries, stops, trend exits) pay
`risk.slippage_bps` while take-profit limits fill at the limit or better; stops
are gap-aware (a bar opening through the stop fills at the open); the entry
bar's own range is checked; and when one bar covers both stop and take-profit,
the stop is assumed to fill first. Equity is marked to market every bar, so
drawdown/Sharpe include open-trade excursions. Spot-only (no leverage; equity
can't go negative).

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
