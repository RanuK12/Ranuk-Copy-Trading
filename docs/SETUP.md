# Setup Guide

Step-by-step configuration for the Polymarket multi-strategy bot.

---

## 1. Prerequisites

* Python **3.11+** (3.9 is the absolute minimum; 3.11 recommended)
* A machine with persistent uptime — Mac Mini, small VPS, or similar
* A Polymarket account funded with USDC on Polygon
* (Optional) Supabase project and Telegram bot for dashboards and alerts

---

## 2. Clone & install

```bash
git clone https://github.com/RanuK12/Ranuk-Copy-Trading.git
cd Ranuk-Copy-Trading

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy the env template and fill in your values:

```bash
cp .env.example .env
$EDITOR .env
```

Running the test suite is a good smoke check:

```bash
pytest -q
```

---

## 3. Polygon RPC (private + redundant)

A private, low-latency RPC is **required** — public endpoints will add
hundreds of milliseconds and will lose the race on arbitrage opportunities.

The bot supports *two* providers with automatic failover. Pick any two:

### Alchemy
1. <https://dashboard.alchemy.com> → create an app.
2. Chain = **Polygon**, Network = **Polygon Mainnet**.
3. Copy the HTTPS and WSS URLs into `.env`:
   ```
   ALCHEMY_HTTP_URL=https://polygon-mainnet.g.alchemy.com/v2/<KEY>
   ALCHEMY_WSS_URL=wss://polygon-mainnet.g.alchemy.com/v2/<KEY>
   ```

### QuickNode
1. <https://www.quicknode.com> → create an endpoint on **Polygon
   Mainnet**. Pick the region closest to your bot:
   * Europe-based Mac Mini → Frankfurt or Amsterdam
   * US-based VPS → N. Virginia or Oregon
2. Copy URLs into `.env`:
   ```
   QUICKNODE_HTTP_URL=https://<your-endpoint>.polygon-mainnet.quiknode.pro/<KEY>/
   QUICKNODE_WSS_URL=wss://<your-endpoint>.polygon-mainnet.quiknode.pro/<KEY>/
   ```

If only one provider is configured the bot runs on that single endpoint.
If none is configured, the bot falls back to the public RPC and prints a
warning — not recommended for live trading.

### Why WSS matters
The current scanner uses HTTP polling for simplicity; the WSS URL is
reserved for the CLOB user-channel subscription and for Polygon log
subscriptions used by the DipArb strategy. Both let you detect on-chain
fills in tens of milliseconds instead of the ~300-800 ms you'd see with
HTTP polling.

---

> 🇪🇸 **Si hablás español y es tu primera vez conectando una cuenta de
> Polymarket**, mirá [CONECTAR_WALLET.md](CONECTAR_WALLET.md) antes de
> seguir — cubre paso a paso cómo obtener tu Funder, tu private key,
> depositar USDC por la red correcta y validar el vínculo en paper mode.

## 4. Polymarket API keys

The bot authenticates to the CLOB with your EOA private key and Polymarket
proxy (funder) address.

* **`POLY_PRIVATE_KEY`** — the EOA that signs orders. Never share this.
* **`POLY_FUNDER`** — your Polymarket proxy wallet address (starts with
  `0x` and is visible on your profile URL).
* **`POLY_SIGNATURE_TYPE`**:
  * `0` — standard EOA (MetaMask / hardware wallet)
  * `1` — email / Magic wallet (default for most users)
  * `2` — browser-wallet proxy

The `py-clob-client` derives or creates API credentials on first run via
`create_or_derive_api_creds()`; you do not have to manually set a
`POLY_API_KEY` unless you already have one.

**Discovering market & token IDs:**

```bash
# Top 5 active markets
curl -s "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=5" \
  | jq '.[0] | {question, conditionId, clobTokenIds, slug}'

# Current price of a YES token
curl -s "https://clob.polymarket.com/price?token_id=<YES_TOKEN_ID>&side=BUY" | jq

# Trades from a smart-money wallet
curl -s "https://data-api.polymarket.com/trades?user=<0xWALLET>&limit=20" | jq
```

You don't need to paste IDs into `.env` — the scanner discovers them
automatically from Gamma. You *do* need to paste wallet addresses for the
`smart_copy` strategy.

---

## 5. Supabase (optional — dashboards & audit log)

The bot works fully offline; Supabase is for cross-host visibility.

1. <https://supabase.com> → create a new project.
2. Go to **Project settings → API** and copy the `URL` + `service_role`
   key (**not** `anon`) into `.env`:
   ```
   SUPABASE_URL=https://<project>.supabase.co
   SUPABASE_KEY=<service_role_key>
   ```
3. Run this SQL in the Supabase SQL editor:

   ```sql
   create table fills (
     id            uuid primary key default gen_random_uuid(),
     ts            timestamptz default now(),
     strategy      text not null,
     market_id     text not null,
     status        text not null,
     pnl_usdc      numeric,
     reason        text,
     details       jsonb
   );
   create index on fills (strategy, ts desc);
   create index on fills (market_id);

   create table strategy_stats (
     strategy   text primary key,
     trades     integer,
     wins       integer,
     losses     integer,
     pnl_usdc   numeric,
     updated_at timestamptz default now()
   );
   ```

4. The bot mirrors every fill to this table. It does **not** read from
   Supabase for trading decisions — the local `bot_state.json` is the
   source of truth, so Supabase outages can never break trading.

---

## 6. Telegram alerts + kill switch

Create a Telegram bot via [@BotFather](https://t.me/BotFather), then:

```
TELEGRAM_BOT_TOKEN=123456:ABC-your_bot_token
TELEGRAM_CHAT_ID=123456789   # your chat or group ID
```

Send the bot a `/start` once so it can message you back. Available
commands:

| Command           | Effect                                             |
|-------------------|----------------------------------------------------|
| `/status`         | Markdown summary: mode, equity, PnL, strategies    |
| `/pnl`            | Quick one-line PnL                                 |
| `/emergencystop`  | Activate kill switch (blocks all new orders)       |
| `/stop`           | Alias for `/emergencystop`                         |
| `/resume`         | Release kill switch                                |
| `/start`          | Alias for `/resume`                                |

If `TELEGRAM_BOT_TOKEN` is empty the bot silently skips outbound messages
and does not start the command listener.

---

## 7. Running — paper mode first

Every new deployment **must** run in paper mode for at least 24 hours to
validate strategy selection and calibrate slippage settings. Per the spec,
only strategies exceeding a 75% win rate in backtest should be promoted
to `STRATEGIES_ENABLED`.

```bash
# .env must have MODE=paper
python main.py
```

You should see:

* A Rich dashboard with strategies / queue / fills / connectivity panels
* One log line per scan (every 5 s by default)
* `[SIMULADO]` lines for each replicated trade with theoretical PnL

When happy, flip `MODE=live` in `.env` and restart. The bot will refuse
to start in live mode if `POLY_PRIVATE_KEY` or `POLY_FUNDER` are missing.

---

## 8. Running 24/7 with PM2

```bash
# Install PM2 if you don't have it
npm install -g pm2

# Edit ecosystem.config.js if your venv path differs
pm2 start ecosystem.config.js

# Persist across reboots
pm2 save
pm2 startup              # follow the printed command

# Useful
pm2 logs polybot
pm2 restart polybot
pm2 stop polybot
```

Logs land in `./logs/polybot.{out,err}.log` and are rotated by PM2.

---

## 9. Allowances (live mode only)

If you use an **EOA** (MetaMask / hardware wallet) rather than an email
proxy wallet, you must approve the CTF exchange contracts to spend your
USDC and conditional tokens **once**:

* USDC token: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`
* Conditional tokens: `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`

Spenders to approve (all three):

* `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` (CTF Exchange, legacy binary)
* `0xC5d563A36AE78145C45a50134d48A1215220f80a` (CTF Exchange, neg-risk)
* `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` (Neg-risk adapter)

Email / Magic proxy wallets (`POLY_SIGNATURE_TYPE=1`) do this
automatically and require no manual action.

---

## 10. Troubleshooting

| Symptom                                | Likely cause + fix                                                   |
|----------------------------------------|----------------------------------------------------------------------|
| `Cannot reach Polygon RPC`             | Wrong `ALCHEMY_HTTP_URL` / quota exhausted / wrong chain             |
| `CLOB client authenticated` never prints | You are in paper mode (expected) or `POLY_PRIVATE_KEY` is wrong    |
| Dashboard is blank                     | `LOG_LEVEL=DEBUG` — switch to `INFO` to re-enable Rich Live          |
| `Too many API errors` -> global pause  | Public RPC or bad endpoint; check `.env` and provider quotas         |
| Telegram commands ignored              | Wrong `TELEGRAM_CHAT_ID` (must be your chat with the bot)            |
| Slippage skips every trade             | Your RPC is too slow — upgrade to Alchemy/QuickNode                  |

---

## 11. Where to go next

* `docs/STRATEGIES.md` — which strategy to enable when, and what to
  expect in terms of win rate / PnL / risk.
* `bot/backtest/engine.py` — plug in historical snapshots to qualify a
  strategy before enabling it live.
* `tests/` — a good starting point if you want to tune thresholds
  without breaking the risk manager.
