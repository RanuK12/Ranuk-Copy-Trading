# Low-Budget Guide ($20–$300)

The v2 defaults assume you're running the bot with $1,000+ of capital.
This guide covers how to operate safely with **$20–$30** and scale up
from there. The v3 `BudgetProfile` system does most of the heavy
lifting automatically — this doc explains what it does so you can
trust it.

---

## Budget tiers at a glance

`TOTAL_CAPITAL_USDC` in your `.env` is the single source of truth.
The bot auto-classifies it:

| Capital      | Tier       | Trade size     | Allowed strategies                              | Daily loss cap |
|--------------|------------|----------------|-------------------------------------------------|---------------:|
| ≤ $50        | **micro**  | 25% of capital | `tail_end` only (+ optional `sniper`)           | 10% of capital |
| $50–$300     | **small**  | 10% of capital | `tail_end`, `smart_copy`, `sniper`              | 7% of capital  |
| $300–$5,000  | standard   | $20            | Full 7-strategy set (v2 defaults)               | 5% of capital  |
| > $5,000     | large      | $20            | Full set, tighter caps + recommend hardware wallet | 3% of capital  |

On startup you'll see the effective profile printed:

```
💰 Budget profile: [micro]  capital=$30.00
  Recommended per-trade size:  $7.00
  Max exposure per market:     $12.00
  Max exposure per strategy:   $30.00
  Daily loss cap:              $3.00

  Recommended strategies: tail_end
  Not advised at this tier: arbitrage, market_making, micro_spread, dip_arb
```

---

## Why multi-leg strategies are disabled for micro/small budgets

Polymarket's CLOB has an effective **minimum order size of ~$1**. That
matters because:

* **Arbitrage** needs two legs (buy YES + buy NO). With $20 capital and
  a target $10 per market, that's $5 per leg after the 50/50 split.
  Fine in theory — but the whole arb often falls apart if either leg
  fails, and ending up with $5 on one side is basically a coin flip.
* **Market making** needs enough liquid capital for multiple
  **pending** orders on both sides at once. A $30 account with one pair
  of pending GTCs has 100% of its capital locked up before anything
  fills.
* **Micro-spread** gets the 25% high-risk sizing cut on top of the
  profile cap. On micro tier, that's $0.50 per trade — below Polymarket's
  minimum.
* **DipArb** needs a hedge leg sized at 20% of the primary. At $3
  primary, the hedge is $0.60, which is too small to fill.

The `BudgetProfile.forbidden_strategies` list encodes this so the bot
silently drops these strategies on startup instead of failing every
order with "order below minimum".

---

## What a $20–$30 run actually looks like

With `TOTAL_CAPITAL_USDC=25` and `STRATEGIES_ENABLED=tail_end,sniper`:

```
main               Polymarket Multi-Strategy Bot starting | mode=paper | ...
main               💰 Budget profile: [micro]  capital=$25.00
main                 Recommended per-trade size:  $6.00
main                 Daily loss cap:              $2.50
main                 Recommended strategies: tail_end
main                 Not advised at this tier: arbitrage, market_making, micro_spread, dip_arb
main               Loaded 2 strategies: ['tail_end', 'sniper']
scanner            Scanner started (interval=5s, enrich_top=40)
executor           Executor started mode=paper

[after some time...]

tail_end      market=will-jets-win-nov-sunday side=YES price=0.94 -> [SIMULADO]
  legs=1 entrada=0.94 p&l=+0.36 USDC
sniper        market=some-long-shot ladder=0.01,0.02 -> [SIMULADO]
  placed 2 resting GTC legs totalling $4.80
```

Expected daily PnL: **$0.30–$1.50** on a good day. That's not
life-changing in absolute terms, but at $25 capital it's a
**1.2%–6% daily return** — which compounds.

---

## Realistic expectations

* **You will NOT generate $100/day from $30.** Polymarket liquidity
  and order minimums put a hard ceiling on how much a small account
  can trade.
* **You WILL learn the system cheaply.** The micro tier is designed to
  validate your wallet/RPC/notification setup with real money at stake
  but without catastrophic risk.
* **Compound aggressively.** Once you cross $50, the small tier unlocks
  smart_copy and sniper. At $300 you're at the standard tier with the
  full strategy set. Plan your funding cadence around those thresholds.

---

## Step-by-step setup for a $20–$30 account

### 1. Fund your Polymarket proxy wallet

See `docs/CONECTAR_WALLET.md` — send USDC via **Polygon** (not
Ethereum) to the address shown on your Polymarket profile. Send $10
first to confirm, then the rest.

### 2. Configure `.env`

```bash
MODE=paper
LOG_LEVEL=INFO

# Key config for micro tier
TOTAL_CAPITAL_USDC=25
DEFAULT_TRADE_SIZE_USDC=20   # will be clamped to profile cap
STRATEGIES_ENABLED=tail_end  # bot will drop forbidden ones automatically

# Use encrypted wallet (recommended even for small budgets)
WALLET_MODE=auto
# After `python main.py --setup-wallet`:
WALLET_PASSWORD=your-strong-password

POLY_FUNDER=0xYourProxyWalletAddress
POLY_SIGNATURE_TYPE=1

# Desktop notifications (Telegram optional)
NOTIFY_DESKTOP=true
NOTIFY_SOUND=true
NOTIFY_TELEGRAM=false
```

### 3. Run in paper mode for 48 hours

```bash
python main.py --setup-wallet   # one-time
python main.py                  # starts in paper mode
```

Watch the Textual TUI. If you see `tail_end` firing paper fills with
positive PnL, the setup is healthy.

### 4. Flip to live

```bash
# edit .env -> MODE=live
python main.py
```

Watch for the `CLOB client authenticated` log line — that confirms the
wallet + funder + signature type are correct.

### 5. Graduate to the next tier

Once your paper ledger (or live ledger) shows a positive PnL over 7+
days, top up the wallet and restart. The BudgetProfile re-classifies
immediately.

---

## Commands that help on small budgets

From the TUI command bar (or `python main.py --command "..."`):

```
/budget                 show current tier and recommendations
/status                 quick equity + PnL per strategy
/pnl week               last 7 days of fills
/strat sniper on        enable sniper manually (if not in recommendations)
/size tail_end 50       halve tail_end sizing if you want even smaller bets
/pause                  stop everything immediately
/resume 2h              resume, auto-pause after 2 hours
```

---

## Red flags to watch for

* **Every trade is skipped with `below_min_size`**: your tier is too
  small for the strategy. The bot should have pruned it — if not, file
  an issue.
* **Daily loss cap hits within an hour**: either slippage is too loose
  (tighten `MAX_SLIPPAGE` to `0.01`) or the strategy is misclassified
  for the current market conditions. Switch to paper for a day.
* **Win rate below 60% on `tail_end`**: your `TAIL_END_MIN_PRICE` may
  be too low. Raise it to `0.95` so you only buy the strongest
  conviction markets.

---

## Scaling up

A typical 90-day progression:

```
day   1 -  7  : paper mode with $25, validate setup
day   8 - 30  : live $25, micro tier, tail_end only, compound
day  31 - 60  : live $75-$150, small tier, add smart_copy + sniper
day  61 - 90  : live $300+, standard tier, full strategy set
```

At each tier the `BudgetProfile` auto-tunes sizing and caps. You never
have to edit anything except `TOTAL_CAPITAL_USDC` and reload.

---

## Related docs

* `docs/SETUP.md` — general installation and RPC setup.
* `docs/CONECTAR_WALLET.md` — Spanish walkthrough of wallet linking.
* `docs/WALLET_SECURITY.md` — Tier 1/2/3/4 threat model.
* `docs/STRATEGIES.md` — what each strategy actually does.
