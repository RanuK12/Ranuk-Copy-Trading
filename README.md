# Polymarket Copy-Trading Bot

A lightweight Python bot that mirrors the trades of up to **10 "smart money"
wallets** on [Polymarket](https://polymarket.com) (Polygon PoS) with a fixed
USDC notional per trade, hard slippage limits, deduplication and a built-in
paper-trading mode.

```
┌─────────────────────────┐     poll / wss       ┌─────────────────────────┐
│   Smart-money wallets   │ ─────────────────▶   │   copy_trading_bot.py   │
│   (Polymarket leaders)  │                      │  (async watcher loops)  │
└─────────────────────────┘                      └───────────┬─────────────┘
                                                             │
                                   slippage + dedup checks   ▼
                                                    ┌──────────────────┐
                                                    │  py-clob-client  │
                                                    │  FOK market buy  │
                                                    └────────┬─────────┘
                                                             │
                                                   Polygon (CTF Exchange)
```

Implementation reference: `copy_trading_bot.py` (single file, ~280 SLOC).

---

## 1. Features

| Requirement                                 | Where it lives in the code                            |
| ------------------------------------------- | ----------------------------------------------------- |
| Monitor up to 10 wallets                    | `CFG.smart_wallets` (hard-capped to 10)               |
| Detect filled orders in real time           | `watch_wallet()` polls `data-api.polymarket.com/trades` |
| Copy YES/NO position with fixed 20 USDC     | `TRADE_AMOUNT_USDC` + `place_market_order()`          |
| 2% max slippage guard                       | `process_trade()` checks `/price` before executing    |
| Duplicate-market prevention                 | `STATE.copied_markets` persisted in `bot_state.json`  |
| Paper-trading mode                          | `PAPER_TRADING=true` -> `simulate_trade()`            |
| Block-by-block terminal logs                | `block_heartbeat()` + `rich` structured logger        |
| Env-driven config, no hard-coded secrets    | `.env` loaded via `python-dotenv`                     |
| Async / low-latency                         | `asyncio.to_thread` per-wallet watcher                |

---

## 2. Quickstart

```bash
# 1. Clone and enter the project
git clone https://github.com/RanuK12/Ranuk-Copy-Trading.git
cd Ranuk-Copy-Trading

# 2. Virtual env + deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
$EDITOR .env        # fill in RPC, PRIVATE_KEY, SMART_WALLETS, ...

# 4. Run (PAPER_TRADING=true by default)
python copy_trading_bot.py
```

Stop with `Ctrl+C`; state is persisted to `bot_state.json` so duplicates are
remembered across restarts.

---

## 3. Finding market IDs and token IDs

The bot uses two Polymarket identifiers:

* **`conditionId`** — the 0x-prefixed 32-byte hash that identifies a market
  (used for dedup).
* **`token_id`** (a.k.a. `asset`) — the uint256 ERC-1155 ID of a specific
  outcome (YES or NO). This is what the CLOB needs for order placement.

You don't have to hard-code them; the bot discovers both automatically from
the Data-API `/trades` response of each monitored wallet. But when you want
to inspect markets manually:

### Gamma API (public, no auth)

```bash
# Most-traded currently-open markets:
curl -s "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=5" | jq '.[0] | {question, conditionId, clobTokenIds, slug}'

# Search by slug:
curl -s "https://gamma-api.polymarket.com/markets?slug=will-bitcoin-hit-200k-in-2026" | jq
```

Response contains:

* `conditionId`  -> use for dedup / activity queries
* `clobTokenIds` -> JSON array `[YES_token_id, NO_token_id]`
* `slug`         -> human-friendly URL name

### CLOB API (public read + authenticated trade)

```bash
# Current price (0.00 - 1.00) for a specific token/outcome:
curl -s "https://clob.polymarket.com/price?token_id=71321045679252...&side=BUY" | jq

# Full orderbook:
curl -s "https://clob.polymarket.com/book?token_id=71321045679252..." | jq
```

### Data-API: who's trading what

```bash
# Trades from a single wallet (reverse-chron):
curl -s "https://data-api.polymarket.com/trades?user=0xWALLET&limit=20" | jq

# Top holders of a market (great for sourcing smart-money candidates):
curl -s "https://data-api.polymarket.com/holders?market=0xCONDITIONID&limit=20" | jq
```

---

## 4. Choosing "smart money" wallets

The bot is only as good as the wallets you mirror. Start from the public
leaderboard (`https://polymarket.com/leaderboard`) and **filter aggressively**:

1. **Consistency over jackpots.** Prefer wallets with positive ROI in 3 or
   4 of the last 6 months rather than one wallet that 50x'd a single bet.
2. **Category focus.** Sports and crypto markets have tighter spreads and
   resolve more often; wallets that specialize there tend to be edge-based
   rather than luck-based.
3. **Volume + turnover.** A wallet with 200+ closed markets and a 58–65%
   win rate beats a wallet with 10 closed markets at 80%.
4. **Recent activity.** Ignore dormant wallets — a bot that mirrors
   inactivity adds only latency risk.
5. **Independence.** Diversify across 5-10 wallets so correlated sharp
   drawdowns don't wipe you out in a single event.

Paste the addresses (comma-separated, max 10) into `SMART_WALLETS=` in `.env`.

---

## 5. RPC setup — why latency is everything

Polygon blocks are ~2 seconds, but price-moving trades propagate to every
Polymarket bot in under a second. A public RPC (`https://polygon-rpc.com`)
will frequently add 300–1500 ms of round-trip, which is enough to lose the
race and trip the 2% slippage guard. Use a private node:

### Alchemy (recommended, free tier is enough for this bot)

1. Create an account at <https://dashboard.alchemy.com>.
2. Create a new app: **Chain = Polygon, Network = Polygon Mainnet**.
3. Copy the **HTTP** and **WSS** endpoints into `.env`:

   ```bash
   POLYGON_HTTP_RPC=https://polygon-mainnet.g.alchemy.com/v2/<API_KEY>
   POLYGON_WSS_RPC=wss://polygon-mainnet.g.alchemy.com/v2/<API_KEY>
   ```

### QuickNode

1. Sign up at <https://www.quicknode.com>.
2. Launch an endpoint on **Polygon Mainnet** (pick the region closest to
   your bot — for a Mac Mini in Europe use Frankfurt/Amsterdam; for US use
   N. Virginia or Oregon).
3. Copy the HTTPS + WSS URLs into `.env`.

### Chainstack / Ankr / Blast

Same pattern: create a Polygon mainnet endpoint, grab HTTPS + WSS, paste in
`.env`. Any provider with a dedicated node and <50 ms to your machine is
fine.

**Why WSS matters:** the HTTP polling used by default is simple and robust,
but the `data-api.polymarket.com/trades` endpoint is rate-limited and adds
~150-400 ms of overhead. For sub-100 ms detection, upgrade to:

* **CLOB user channel** (`wss://ws-subscriptions-clob.polymarket.com/ws/user`)
  — push notifications for order fills on *specified* proxy wallets. See
  <https://docs.polymarket.com/market-data/websocket/user-channel>.
* **Polygon WSS logs** (your `POLYGON_WSS_RPC`) — subscribe to
  `OrderFilled` events on the two CTF Exchange contracts:
    * Binary markets: `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`
    * Neg-risk markets: `0xC5d563A36AE78145C45a50134d48A1215220f80a`

Both push events end-to-end in tens of milliseconds. The current bot ships
with HTTP polling (simpler + resilient); porting to WSS is ~30 lines in
`watch_wallet()` — swap the `asyncio.to_thread(get_recent_trades, ...)` call
for an `async for msg in websocket:` loop that filters on the tracked
proxy-wallet addresses.

---

## 6. Going live — checklist

1. **Fund the Polymarket proxy wallet** (not the signing EOA) with enough
   USDC. The bot will spend `TRADE_AMOUNT_USDC` per copied market.
2. **Approve allowances** (one time, only if you use a MetaMask/EOA
   `signature_type=0` — email/Magic wallets do this automatically). Token
   list and spender contracts are documented in the py-clob-client README
   (`USDC.e 0x2791Bca1…`, `ConditionalTokens 0x4D97DCd9…`, approve the two
   exchanges above plus `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`).
3. Start in paper mode for at least 24 hours to validate your wallet
   selection and slippage settings:

   ```bash
   PAPER_TRADING=true python copy_trading_bot.py
   ```

4. Inspect `bot_state.json` after the paper run. The `paper_positions`
   array gives you a first-order ROI estimate vs. the detected entry price.
5. When happy, flip `PAPER_TRADING=false` and restart.

---

## 7. Safety notes

* The `PRIVATE_KEY` in `.env` controls real funds — never commit it, never
  paste it in chat. `.gitignore` already excludes `.env`.
* `MAX_SLIPPAGE=0.02` is the *hard* cap: the bot skips anything above. It
  does **not** slide your entry price, it aborts. This is by design.
* The 20 USDC default is a floor that clears typical min-size constraints on
  Polymarket orderbooks; going lower may cause FOK rejections.
* Duplicate-prevention is per **`conditionId`**, not per token — meaning the
  bot will not buy YES on a market where it already holds NO (or vice
  versa). Clear `bot_state.json` if you want to reset.
* This project is a personal-use tool. It is **not** financial advice, and
  running it on wallets you don't own is a good way to lose money fast.

---

## 8. References

Architecture and API patterns were informed by these open-source projects,
which are worth reading for a deeper dive:

* <https://github.com/HKUDS/Vibe-Trading>
* <https://github.com/GiordanoSouza/polymarket-copy-trading-bot>
* <https://github.com/direkturcrypto/polymarket-terminal>
* Official Polymarket docs: <https://docs.polymarket.com>
* Data-API reference (community): <https://gist.github.com/shaunlebron/0dd3338f7dea06b8e9f8724981bb13bf>
