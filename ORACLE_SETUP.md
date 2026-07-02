# Free 24/7 hosting on Oracle Cloud "Always Free"

Oracle Cloud gives a **free-forever** VM (not a trial). It's a real always-on
Linux server that reaches Binance directly — perfect for this bot. This guide
gets you a running VM; then follow [`DEPLOY.md`](./DEPLOY.md) to install and run
the bot on it.

Time: ~20–30 minutes. Cost: $0 (a card is required for identity verification,
but Always Free resources don't bill).

---

## ⚠️ Read this first: pick the right region
You choose a **home region during signup and it CANNOT be changed later.**
Binance geo-restricts some regions/IPs, so **pick a region in a country where
binance.com works** (avoid the US). Examples that commonly work: UK, Germany
(Frankfurt), Singapore, Tokyo, Mumbai. If unsure, pick a major non-US region.

---

## 1. Create the account
1. Go to https://www.oracle.com/cloud/free and click **Start for free**.
2. Fill in details, **choose your home region carefully** (see warning above).
3. Verify with a card. You won't be charged for Always Free resources.

## 2. Create the VM instance
1. In the Oracle Cloud console: **☰ Menu → Compute → Instances → Create instance**.
2. **Image:** click **Edit** on "Image and shape" → **Change image** → **Canonical Ubuntu** (22.04).
3. **Shape:** **Change shape** → **Ampere** is bigger but often capacity-limited;
   the reliable pick is **Specialty and previous generation → VM.Standard.E2.1.Micro**
   (AMD, 1 OCPU / 1 GB). Both are **"Always Free-eligible"** — look for that label.
4. **SSH keys:** choose **Generate a key pair for me** and **download both** the
   private and public keys (you need the private key to log in). Or paste your own
   public key.
5. Leave networking defaults (it creates a VCN with a public IP and open SSH).
6. Click **Create**. Wait until the instance is **Running** and note its
   **Public IP address**.

## 3. Connect via SSH
On any machine with SSH (or use the browser-based **Cloud Shell** in the console):
```bash
chmod 600 /path/to/your-private-key.key
ssh -i /path/to/your-private-key.key ubuntu@YOUR_PUBLIC_IP
```
(Default Ubuntu user is `ubuntu`.)

## 4. Verify Binance is reachable from this VM
Before installing anything, confirm the region isn't blocked:
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://api.binance.com/api/v3/ping
```
- `200` → you're good, continue.
- `451` / `403` → this region is geo-blocked. The home region can't be changed,
  so you'd need a new account in a different region. (Better to find out now.)

## 5. Install and run the bot
Follow [`DEPLOY.md`](./DEPLOY.md) from **step 1** (apt install) onward:
install Python, clone the repo, set up `.env` with **testnet** keys first,
`--scan` to smoke-test, run the **backtest**, then the systemd `--loop` service,
then flip to live.

---

## Tips
- **Avoid idle reclamation:** Oracle may reclaim Always Free compute instances
  that look idle for ~7 days. The bot's constant polling usually keeps it active,
  but to be safe you can upgrade the account to **Pay As You Go** in Billing —
  this **keeps Always Free free** (no charges for Always Free usage) and removes
  the idle-reclamation policy.
- **Keep it cheap and safe:** only fund the exchange with what you're willing to
  lose; enable spot trading and **disable withdrawals** on the live API key;
  IP-whitelist the VM's public IP.
- **Monitoring:** add a Telegram token to `.env` (see `DEPLOY.md`) so you get
  entry/exit/error alerts on your phone.
