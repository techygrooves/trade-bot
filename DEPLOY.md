# Deploying the bot on an always-on host (VPS)

This cloud session can't reach Binance (its egress is locked down) and gets
reclaimed when idle, so it's not a place to run a live bot. A small Linux VPS is
the standard home for a 24/7 trading bot — it reaches Binance directly with **no
allowlist to configure**, and it stays up so your software stop-loss is always
enforced.

Budget: a $4–6/month VPS (1 vCPU / 1 GB RAM) is plenty.

> ⚠️ **Region matters.** binance.com geo-restricts some countries and many
> datacenter IPs. Pick a VPS region where binance.com works (e.g. **not** the US).
> If `--scan` returns a "restricted location" error, change the VPS region.

---

## 1. Provision the server
Create an **Ubuntu 22.04/24.04** VPS (DigitalOcean, Hetzner, Vultr, Linode, etc.)
and SSH in as a sudo user.

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
```

## 2. Get the code
```bash
git clone https://github.com/techygrooves/trade-bot.git
cd trade-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Configure secrets (testnet first)
```bash
cp .env.example .env
nano .env
```
Start on the **testnet** (free, no real funds). Get keys at
https://testnet.binance.vision and set:
```
BINANCE_TESTNET=true
BINANCE_API_KEY=your_testnet_key
BINANCE_API_SECRET=your_testnet_secret
# Optional alerts (create a bot via @BotFather, get chat id from @userinfobot):
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```
`config/config.yaml` already defaults to binance.com (`exchange_tld: "com"`),
`fixed_budget` sizing, and a 10 USDT budget per trade — tune as you like.

## 4. Smoke-test
```bash
python -m src.bot --scan     # should connect and print a signal per symbol
python -m src.bot --once     # one decision cycle on the testnet
```
If `--scan` connects and prints signals, you're wired up correctly.

## 5. (Recommended) Run the real backtest
On the VPS you can finally validate the edge on real history:
```bash
python -m src.backtest --symbol BTCUSDT --start 2023-01 --end 2024-12
```
Look at win rate, profit factor, and **max drawdown** before risking money.

## 6. Run continuously with systemd
Create `/etc/systemd/system/trade-bot.service` (adjust the user/path):
```ini
[Unit]
Description=Trade bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/trade-bot
ExecStart=/home/YOUR_USER/trade-bot/.venv/bin/python -m src.bot --loop
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now trade-bot
journalctl -u trade-bot -f        # watch live logs
```
`Restart=always` means it comes back after a crash or reboot — and because the
open position is persisted to `state/`, it resumes instead of re-buying.

## 7. Go live (only after testnet looks right)
Edit `.env`:
```
BINANCE_TESTNET=false
BINANCE_API_KEY=your_LIVE_binance_com_key
BINANCE_API_SECRET=your_LIVE_binance_com_secret
```
Then restart: `sudo systemctl restart trade-bot`.

Fund the account with the small amount you're willing to risk (e.g. 10–15 USDT;
keep a little above the 10 USDT trade budget for fees).

## Security checklist
- Live API key: **enable spot trading, DISABLE withdrawals**, IP-whitelist the
  VPS IP if your account supports it.
- Never commit `.env` (it's gitignored). Keep keys only on the server.
- `chmod 600 .env` so only your user can read it.
- Start tiny. Watch `journalctl` and your Telegram alerts for the first days.

## Honest reminders
- Stops are **software-managed** — they only work while the service is running.
  systemd `Restart=always` covers crashes; keep the VPS itself up.
- The strategy logic is unit-tested and identical between backtest and live, but
  profitability is only proven once you run the backtest (step 5) on real data.
