# Strategy Guide

One chapter per strategy. Each covers:

* **What it does** — the trading thesis.
* **When to enable** — market conditions where it shines.
* **Expected performance** — rough win-rate and PnL behavior.
* **Risks** — what can go wrong.
* **Config knobs** — the `.env` variables that tune it.

All strategies go through the same pipeline:

```
Scanner (5s) -> Strategy.generate() -> OpportunityQueue -> Executor (risk gate + slippage + exec)
```

Strategies never place orders directly; the executor is the single point
of execution and the only place that touches real funds.

---

## A. Arbitrage (`arbitrage`) — priority 0

**Thesis.** On binary markets, `YES_ask + NO_ask` must be ≥ $1, because
exactly one share of each outcome is guaranteed to pay $1 at resolution.
When the sum is < $1, you can buy both sides and lock in a risk-free
profit at resolution.

**When to enable.** Always — this is the lowest-risk strategy in the
pack. The tradeoff is frequency: real arbs last seconds, so you need a
fast RPC and good luck with timing.

**Expected performance.**
* Hit rate when an opportunity exists: ~30-50% (others will race you).
* Avg edge captured when you win: 1-4 cents per dollar deployed.
* Realistic daily fill count: 1-10 on a well-tuned setup.

**Risks.**
* One-legged fills. The executor dispatches both legs in parallel with
  FOK; a partial fill releases exposure and alerts you on Telegram.
* Negative-risk multi-outcome markets can create phantom arbs that
  disappear when the full basket is priced — the scanner filters
  `negative_risk` markets but double-check in your env.

**Config.**
```
ARB_MIN_PROFIT=0.01          # 1% minimum edge (after gas)
ARB_MIN_VOLUME_USDC=1000     # liquidity floor
```

---

## B. Tail-End (`tail_end`) — priority 10

**Thesis.** A market priced at $0.95 with two days to go has already
decided. Buying the leading outcome at $0.95 and holding to resolution
pays 5 cents per share — consistent ~5% per trade with almost no
holding risk.

**When to enable.** All the time, but especially the week before major
events (sports finals, scheduled announcements). Avoid the day of
resolution for markets with subjective criteria (politics, court
rulings) where a late oracle surprise is more likely.

**Expected performance.**
* Win rate: 85-95% when thresholds are tight.
* Avg edge per trade: 2-7%.
* Daily fill count: 5-20, scales with how many markets you track.

**Risks.**
* Oracle surprises near resolution (rare but they happen — see UMA
  optimistic-oracle disputes).
* Wicks below the stop-loss due to thin books.

**Config.**
```
TAIL_END_MAX_DAYS=7          # ignore markets > 7 days out
TAIL_END_MIN_PRICE=0.93      # only buy above this price
TAIL_END_STOP_LOSS=0.88      # operator's informational stop
```

---

## C. Micro-Spread (`micro_spread`) — priority 20

**Thesis.** Outcomes priced $0.05 - $0.10 often have 5-10 cent spreads.
Posting a limit buy at the bid and letting the book crawl toward us
yields a large *percentage* return (10-20% in pennies per cycle).

**When to enable.** During active market hours on high-activity markets.
Capital is auto-capped at `MICRO_CAPITAL_PCT` (default 20%) because this
is the most volatile strategy in the pack.

**Expected performance.**
* Win rate per cycle: 55-70%.
* Avg edge per winning cycle: 15-40% of the deployed notional.
* Daily cycles: 20-200 depending on market coverage.

**Risks.**
* Adverse selection — you often get lifted right before a negative news
  event. The risk manager's 25% sizing cut for high-risk strategies is
  doing real work here.
* You're stuck with tokens if the market fails to attract buyers.

**Config.**
```
MICRO_PRICE_MIN=0.05
MICRO_PRICE_MAX=0.10
MICRO_MIN_SPREAD=0.05
MICRO_MIN_VOLUME_PER_MIN=500
MICRO_CAPITAL_PCT=0.20
```

---

## D. DipArb (`dip_arb`) — priority 15

**Thesis.** Polymarket 15-min crypto markets occasionally dump >15% in
seconds because a single size-aware seller walks down thin books. If the
actual spot price on Binance barely moved, the Polymarket move is a
mispricing that reverts within seconds.

**When to enable.** Whenever you have Binance connectivity and care
about short-timeframe crypto price markets.

**Expected performance.**
* Win rate: 55-65% (with Binance CEX confirmation).
* Avg edge per trade: 3-7%.
* Fills per day: very bursty — 0 most hours, 5-10 during volatility.

**Risks.**
* Regime changes where Polymarket *leads* Binance (very rare but
  possible around macro news). The 20% opposite-side hedge limits the
  damage.
* Binance API throttling cutting off CEX confirmation; the strategy
  silently skips when Binance data is unavailable.

**Config.**
```
DIP_MIN_DROP=0.15            # 15% drop in 3 seconds
DIP_LOOKBACK_SECONDS=3
```

---

## E. Smart Copy (`smart_copy`) — priority 30

**Thesis.** The best Polymarket traders have *persistent* edges. Tracking
their trades gives you idea flow — but blindly copying the leaderboard
is a great way to lose money. The strategy only mirrors wallets that
pass a 5-factor performance screen.

**Filters (all must pass).**
* Win rate ≥ 60% over the wallet's recent trade history
* Profit factor ≥ 1.5× (gains / losses)
* Total realized PnL ≥ $500 (filters toy wallets)
* Consistency ≥ 70% of ISO weeks with net positive PnL
* Largest single trade ≤ 30% of total PnL (no whale-lucky wallets)

Wallet scores are cached for 30 minutes to stay inside the Data-API
rate limit.

**When to enable.** Always — but only after you've chosen 5-10 wallets
carefully (see README for criteria). Smaller lists tend to perform
better than large ones.

**Expected performance.**
* Depends entirely on your wallet list. Good curation: 65-80% win rate.
* Avg edge per trade: whatever the source wallet captures, minus our
  slight slippage buffer.

**Risks.**
* Trader turnover — a wallet that passed the filter three months ago
  may no longer be sharp. The score TTL picks this up within 30 min.
* Copy-bots of copy-bots: if a wallet you track is itself copying
  someone, you'll be last in line.

**Config.**
```
SMART_WALLETS=0xaaaa...,0xbbbb...
COPY_MIN_WIN_RATE=0.60
COPY_MIN_PROFIT_FACTOR=1.5
COPY_MIN_TOTAL_PNL_USDC=500
COPY_MIN_CONSISTENCY=0.70
COPY_MAX_SINGLE_TRADE_PCT=0.30
```

---

## F. Market Making (`market_making`) — priority 20

**Thesis.** On crypto 15-min markets, when `YES_bid + NO_bid < $0.98`,
posting GTC bids on both sides and waiting for both to fill yields the
spread plus a maker rebate, for a ~3 cent-per-cycle edge. When both
sides fill, the two outcomes can be merged back to USDC via the CTF
contract.

**When to enable.** On persistent crypto markets where you can tolerate
a lot of pending orders. Less useful if you're hard-limited on API
requests.

**Expected performance.**
* Win rate per round-trip: 60-70%.
* Avg edge per completed cycle: 1-3 cents on a dollar of notional.
* Cycles per day: highly dependent on market volatility.

**Risks.**
* One-sided fills. The strategy refuses to re-enter a market after a
  one-sided cycle until you manually clear the `mm_one_sided::*` tag in
  `bot_state.json`.
* Ghost fills (order response says filled but tokens never arrive) are
  logged but not yet auto-reconciled on-chain. Manual recovery required
  if you see one in the executor logs.

**Config.**
```
MM_MAX_TOTAL_PRICE=0.98
MM_LADDER_LEVELS=3           # reserved for multi-level MM (future work)
```

---

## G. Sniper (`sniper`) — priority 40

**Thesis.** On any liquid market, parking resting GTC limit orders at
extreme discounts ($0.01 / $0.02 / $0.03) occasionally catches panic
dumps or liquidation cascades at fire-sale prices. Low hit rate but
strongly asymmetric upside.

**When to enable.** Always — the sizing is small by default and the
hourly multiplier scales up during peak hours (12-20 UTC) and down
during quiet hours (0-6 UTC).

**Expected performance.**
* Hit rate per day: 0-2 fills, rarely more.
* When filled: 20x-50x returns (at $0.02 in, $0.50+ out at resolution).
* Monthly PnL contribution: small but positive, with long tails.

**Risks.**
* Capital sitting in resting orders is *not* available for other
  strategies until the orders are cancelled.
* Extreme market scenarios where even $0.01 levels get cleared.

**Config.**
```
SNIPER_PRICES=0.01,0.02,0.03
SNIPER_WEIGHTS=0.5,0.3,0.2   # must sum to 1.0
```

---

## Enabling & disabling

```
STRATEGIES_ENABLED=arbitrage,tail_end,smart_copy
```

Add any of `arbitrage, tail_end, micro_spread, dip_arb, smart_copy,
market_making, sniper`.

Recommended progression:

1. Week 1 (paper) — `arbitrage,tail_end` only. Validate signals and
   wallet-funder wiring.
2. Week 2 (paper) — add `smart_copy` once you've chosen a wallet list.
3. Week 3 (paper or live small) — add `market_making` on a single
   crypto market at a time.
4. After a month of consistent paper PnL — add `micro_spread`,
   `dip_arb`, `sniper` as you feel comfortable.

The Rich dashboard's per-strategy win-rate and PnL column is the
single best tool for deciding whether to keep a strategy enabled.
