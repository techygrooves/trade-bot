# Repo Analysis & Next-Phase Plan

_Status as analyzed: Phases 0–3 built, 35/35 unit tests passing. This document
records what the code actually does today, where it diverges from PLAN.md and
from itself, and the recommended order of work to get from "code written" to
"validated strategy running live"._

## 1. What exists and works

- **Signal engine** (`src/strategy/signals.py`): multi-timeframe confluence
  (4h trend filter, 1h EMA-reclaim/MACD trigger, ADX, RSI pullback band,
  volume) implemented as one `compute_features()` shared by live and backtest —
  good design, no rule drift possible at the *entry* level. Lookahead is
  handled correctly (trend bars aligned by close time via `merge_asof`).
- **Backtester** (`src/backtest/engine.py`): event-driven, decisions on closed
  bars, fills at next open, intrabar stop checks, fees both sides, spot-only
  sizing cap.
- **Live engine** (`src/live/trader.py`): exchange-agnostic `step()`, software
  stop/TP, trend-flip exit, daily-loss guard, crash-safe position file,
  Telegram alerts.
- **Tests**: solid unit coverage of indicators, signals, sizing, guard,
  backtest invariants, and the live loop against a fake exchange.

## 2. Critical gaps (ranked)

### G1 — The backtest does not test the live strategy (highest priority)
The live engine exits at a **fixed take-profit of 1.5R** (`live.reward_mult`),
the backtester has **no take-profit at all** (exits only on stop or trend
flip). These are two different strategies. Every backtest number produced today
is invalid as evidence for what `--loop` will actually do. Nothing else should
be tuned or trusted until exit logic is identical in both.

Also worth stating honestly: a hard 1.5R cap with a 2×ATR stop *raises win
rate* but truncates exactly the right-tail winners a trend strategy lives on.
"Win maximum trades" and "maximum expectancy" are different objectives — the
backtest comparison in Phase B below is how we decide, with data, instead of
by preference.

### G2 — Planned exit scheme was never implemented
PLAN.md specifies scaled take-profits (⅓ at 1.5R, ⅓ at 3R, ATR-trail the rest).
Neither engine implements it. This is the single biggest strategy-quality item
outstanding.

### G3 — Stops exist only while the process is alive
Stops/TP are software-checked once per poll (60s) against the last trade price.
No exchange-native OCO order is placed (PLAN.md §4 called for one). If the
host dies with a position open there is **no protection at all**, and even
while running, a fast wick between polls fills worse than the stop. This must
be fixed before real money.

### G4 — The kill switch doesn't work in `--once`/cron mode
`DailyLossGuard` is in-memory only. Every cron invocation constructs a fresh
guard, so cumulative daily loss is always 0 and the halt can never trip —
despite `--once` being documented as "safe for cron". Guard state must be
persisted (same pattern as `live/state.py`).

Related: the guard's reference equity is `trade_budget_usdt` (default $10), so
the "5% daily loss limit" means **$0.50**. Fine by accident for fixed-budget
mode, wrong for `risk_pct` mode; it should reference account equity.

### G5 — Live PnL accounting is wrong
`_manage()` computes `pnl = fill["quote"] - pos.qty * pos.entry_price`:
- ignores entry and exit fees (a ~0.2% round trip is material at 1.5R targets);
- uses `pos.qty` as cost basis even when the actually-sold qty was reduced by
  lot-size rounding or a smaller free balance — PnL is misstated and dust
  accumulates silently.

This feeds the kill switch, so the safety math is wrong too.

### G6 — No validation methodology
No Sharpe (PLAN.md lists it), no per-exit-reason stats beyond a count, no
multi-symbol/portfolio backtest, no parameter sweep, no train/test split or
walk-forward. Tuning on a single in-sample run is how curve-fit bots get
built.

### Smaller items
- Backtest realism: stop fills at the stop price even when the next bar *opens*
  below it (should fill at `min(open, stop)`); the entry bar's own low is never
  checked against the stop; no slippage model.
- Live trades one symbol only (`symbols[0]`); `max_open_positions` is dead
  config.
- Trades aren't logged to CSV (PLAN.md §6).
- Two pandas `FutureWarning`s (`fillna` downcasting) in `ta.py:90` and
  `signals.py:134` — trivial, fix before they become errors on a pandas
  upgrade.
- Note for cloud sessions: `data.binance.vision` must be on the egress
  allowlist for the backtester's download path; otherwise use `--csv`.

## 3. Recommended plan

Order matters: **measure honestly → tune with discipline → harden execution →
soak on testnet → go live small.** Do not reorder C before B — implementing
exits live before knowing which exit scheme wins is wasted work.

### Phase A — Make the backtest tell the truth (~1 day)
1. Add take-profit handling to the backtest engine and parameterize the exit
   scheme so one flag switches between: (a) stop + trend-exit only (current
   backtest), (b) fixed TP at `reward_mult` (current live), (c) scaled TP +
   ATR trailing stop (PLAN.md target).
2. Realism fixes: gap-aware stop fills (`min(open, stop)` / TP analog), check
   the entry bar's own range, add a simple slippage parameter (bps).
3. Metrics: add Sharpe, per-exit-reason PnL breakdown, average holding time,
   and equity sampled per-bar (not only on trade closes) so drawdown is real.
4. Fix the two `FutureWarning`s while in there.

### Phase B — Validate and choose the strategy (~1–2 days)
1. Multi-year (2021–2025 to include a full bear leg), multi-symbol (BTC, ETH,
   +2 large caps) backtests for each exit scheme from A1.
2. Small, honest parameter sweep (ADX threshold, RSI band, ATR mult,
   reward_mult / trail width) with a **train/validation split or rolling
   walk-forward** — pick parameters on train, report on validation only.
3. Decision gate: pick the exit scheme + params by expectancy (R), profit
   factor, and max drawdown — *not* by win rate alone. Record the chosen
   config and its out-of-sample numbers in this file.

**Status: harness built, real-data run pending.**
`src/backtest/experiment.py` + `src/backtest/phase_b.py` implement all three
steps: baseline scheme comparison (default params, train/validation/full
windows), a 108-combo sweep (ADX ∈ {15,20,25}, RSI band ∈ {40–60, 35–65},
stop ∈ {1.5,2,2.5}×ATR, reward_mult ∈ {1.5,2,3} / trail ∈ {2,3}×ATR) scored on
train only, per-scheme champions scored once on held-out validation, and a
decision gate (pooled expectancy > 0, ≥40 train / ≥10 validation trades, worst
single-symbol drawdown ≤ 30%) that ranks by expectancy then profit factor —
win rate is reported but never selected on. Candles are cached under `data/`
so the experiment reruns offline.

The one thing this cloud session cannot do is fetch real candles: the
environment's egress policy blocks `data.binance.vision` (and every other
market-data host). To produce the real report, either allowlist
`data.binance.vision` in the environment's network policy, or run
`python -m src.backtest.phase_b --fetch-only` on any machine with network and
commit nothing — just reuse its `data/` cache. Then:

```bash
python -m src.backtest.phase_b   # writes reports/phase_b/report.md + CSVs
```

**Chosen config (fill in from the real run's report.md):**
- Winner: _pending real-data run_
- Validation expectancy / profit factor / worst max DD: _pending_
- Train→validation expectancy decay: _pending_

### Phase C — Harden live execution to match (~1–2 days)
1. Implement the winning exit scheme in `LiveTrader` (scaled TP legs +
   trailing stop imply partial sells and a mutable stop in `Position` state).
2. Exchange-native protection: after entry, place a real OCO
   (stop-loss-limit + limit TP) on Binance so protection survives the bot
   dying; on each step, reconcile local state against open orders instead of
   only against last price. Cancel/replace on trail updates.
3. Fix PnL: use actual filled qty and include both fees; feed the corrected
   number to the guard.
4. Persist `DailyLossGuard` state to `state/`; reference it to account equity;
   reconcile position file vs. actual balances at startup (warn on mismatch).
5. Log every closed trade to `logs/trades.csv`.

### Phase D — Multi-symbol (optional before live, required before scaling)
Loop `LiveTrader` over all configured symbols, enforce `max_open_positions`,
share the guard across symbols, and make budget allocation explicit.

### Phase E — Testnet soak (1–2 weeks of wall-clock, low effort)
Run `--loop` against `testnet.binance.vision` on an always-on host. Verify at
least one full entry → partial TP → trail → exit cycle, a restart with an open
position, an OCO surviving a deliberate bot kill, and a guard trip. Compare
each live fill against what the backtester would have decided on the same
candles (drift check).

### Phase F — Live, small
Real keys (trade-only, withdrawals disabled, IP-whitelisted), minimum sizes,
daily Telegram P&L summary + heartbeat message so silence itself is an alert.
Scale only when live max drawdown and expectancy track the Phase B numbers.

## 4. What NOT to do
- Don't tune parameters against a single backtest run and ship them.
- Don't chase win rate — a 90% win rate with an untested tail is how accounts
  die; the scaled-exit comparison in Phase B is the honest way to decide.
- Don't run `--loop` from an ephemeral cloud session and call it live trading.
- Don't add pairs/leverage/complexity before Phase E is green.
