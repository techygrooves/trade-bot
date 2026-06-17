# Binance Spot Trend-Momentum Bot — Build Plan

## 1. Objective & honest expectations
- **Goal:** A 24/7 automated spot bot that takes high-probability trend/momentum
  setups, protects capital aggressively, and reports to you via Telegram.
- **Reality check:** "Maximum success rate" = high *win rate per trade* + *strict
  risk control*, not guaranteed profit. The edge comes from (a) only trading
  confirmed trends, (b) cutting losers fast, (c) letting winners run, and
  (d) never risking more than a fixed small % per trade. A 55–65% win rate with a
  >1.5 reward:risk is excellent and survivable; chasing 90% win rates usually
  hides catastrophic tail losses.

## 2. Strategy logic (the edge)
**Markets:** Start with 3–5 high-liquidity USDT pairs (BTC, ETH + 1–2 large caps).
Liquidity = tighter spreads = better fills.

**Timeframe:** 1h primary signal, 4h trend filter (multi-timeframe confirmation
reduces false signals).

**Entry — all must align (confluence):**
- **Trend filter (4h):** price above EMA200 AND EMA50 > EMA200 (only long in
  uptrends — spot has no shorting, so we sit in cash during downtrends).
- **Momentum (1h):** EMA20 crosses above EMA50, OR MACD bullish cross.
- **Strength:** ADX > 20 (confirms a real trend, not chop).
- **Pullback entry:** RSI between ~40–60 (buy strength on a dip, not an
  overbought top).
- **Volume:** entry candle volume ≥ recent average (confirms participation).

**Exit:**
- **Stop-loss:** ATR-based (e.g. 1.5–2× ATR below entry) — adapts to volatility.
- **Take-profit:** scaled — sell ⅓ at 1.5R, ⅓ at 3R, trail the rest with an
  ATR trailing stop.
- **Hard exit:** trend filter flips bearish (4h EMA50 < EMA200) → close position.

## 3. Risk management
- **Per-trade risk:** fixed % of equity (default **1%**). Position size is computed
  *from* the stop distance, not a fixed dollar amount.
- **Max concurrent positions:** cap (e.g. 3) to limit correlation risk.
- **Daily loss limit / kill switch:** if equity drops X% in a day, stop trading.
- **Circuit breaker:** auto-halt on API errors, abnormal volatility, or repeated
  failed orders.
- **No averaging down** on losers. Ever.
- **Min notional / fee awareness:** respect Binance min order sizes; account for
  ~0.1% taker fee (0.075% with BNB) in the R calculation.

## 4. Architecture (Python)
```
trade-bot/
├── config/           # config.yaml, .env (keys, never committed)
├── src/
│   ├── exchange/     # Binance client wrapper (python-binance)
│   ├── data/         # OHLCV fetch + websocket price stream
│   ├── indicators/   # EMA, RSI, MACD, ADX, ATR
│   ├── strategy/     # signal generation (entry/exit rules)
│   ├── risk/         # position sizing, limits, kill switch
│   ├── execution/    # order placement, OCO stops, retries
│   ├── portfolio/    # state, open positions, equity tracking
│   ├── notify/       # Telegram alerts
│   ├── backtest/     # historical simulation + metrics
│   └── bot.py        # main loop / scheduler
├── tests/            # unit tests for strategy + risk math
├── logs/
├── requirements.txt
└── README.md
```

## 5. Build phases
0. **Scaffold (½ day):** repo structure, config/env loading, logging, Binance
   Testnet connection, fetch OHLCV.  ← *this phase*
1. **Indicators & strategy (1 day):** indicators + signal engine as pure functions.
2. **Backtest (1 day):** run over 1–2 yrs history; win rate, profit factor, max
   drawdown, Sharpe. Tune params here, not with real money.
3. **Risk + execution (1 day):** sizing, ATR stops, OCO orders, kill switch.
4. **Paper/testnet (1–2 days running):** full loop on Binance Testnet.
5. **Go live small:** real keys, tiny capital, withdrawals disabled, monitor.
6. **Iterate:** add pairs, refine params from live results.

## 6. Safety & ops
- API keys in `.env` (gitignored); enable trade-only, **disable withdrawals**,
  IP-whitelist if possible.
- Telegram alerts on: entries, exits, errors, daily P&L, kill-switch trips.
- Persistent state so a restart doesn't lose track of open positions.
- All trades logged to CSV for later analysis.

## 7. Success metrics
Win rate, profit factor, max drawdown, average R per trade, Sharpe.
**Max drawdown decides if it's safe to scale.**
