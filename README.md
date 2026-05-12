# Polymarket Multi-Strategy Trading Bot

An autonomous, multi-strategy trading bot for
[Polymarket](https://polymarket.com) on Polygon. It does **not** blindly
copy trades — it scans every active market on a 5-second cadence,
classifies opportunities into seven tactical buckets, and runs them
through a centralized risk manager before touching a single USDC.

```
┌─────────────────────────────────────────────────────────────────┐
│                      MarketScanner (5 s loop)                   │
│     Gamma /markets   +   CLOB /book  →  MarketSnapshot          │
└───────────────┬─────────────────────────────────────────────────┘
                │   (shared read-only snapshot)
   ┌────────────┼────────────┬─────────────┬──────────────┐
   ▼            ▼            ▼             ▼              ▼
Arbitrage  Tail-End    Micro-Spread   DipArb        Smart-Copy ...
(prio 0)   (prio 10)   (prio 20)      (prio 15)     (prio 30)
   │            │            │             │              │
   └─────────→  OpportunityQueue  (min-heap, dedup) ◀──────┘
                             │
                             ▼
                ┌─────────────────────────────┐
                │          Executor           │
                │  risk.allow()  +  slippage  │
                │  paper : [SIMULADO]         │
                │  live  : py-clob-client FOK │
                └─────────────┬───────────────┘
                              │
                 state.json / Supabase / Telegram
```

---

## Highlights

* **7 strategies** with clear priorities — Arbitrage (sum-to-one),
  Tail-End, Micro-Spread, DipArb (crypto + Binance confirmation),
  Smart-Copy (elite-wallet filters), Market-Making (maker rebates on
  15-min crypto), and a deep-discount Sniper ladder.
* **Strict risk manager** — per-market and per-strategy exposure caps,
  daily / monthly loss caps, 10% drawdown triggers 50% sizing, four
  consecutive losses pause a strategy for an hour, API-error streak
  triggers a global pause, and a remote **Telegram kill-switch**.
* **Paper trading first** — `MODE=paper` exercises the full pipeline
  (scanner, strategies, queue, risk, slippage, executor, Telegram,
  Supabase) without a single on-chain transaction.
* **Async, single-process** — one executor consumer, one scanner, one
  strategy loop per enabled strategy. Fits on a Mac Mini or a 1-CPU
  VPS.
* **Rich terminal dashboard** with per-strategy stats, opportunity
  queue, recent fills, and live connectivity.
* **27 passing unit tests** covering the risk manager, priority queue,
  arbitrage, tail-end, smart-copy wallet scoring, and the backtest
  engine.

---

## Quickstart

```bash
git clone https://github.com/RanuK12/Ranuk-Copy-Trading.git
cd Ranuk-Copy-Trading

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# → edit .env: RPC URLs, POLY_PRIVATE_KEY/FUNDER, SMART_WALLETS, capital

# Run in paper mode (default)
python main.py
```

You should see a live Rich dashboard and per-strategy log lines. Stop
with `Ctrl-C`; state is persisted to `bot_state.json`.

Full installation and configuration walkthrough: **[docs/SETUP.md](docs/SETUP.md)**.
Per-strategy explanations and tuning guidance: **[docs/STRATEGIES.md](docs/STRATEGIES.md)**.

---

## Repository layout

```
Ranuk-Copy-Trading/
├── main.py                          # async orchestrator
├── ecosystem.config.js              # PM2 process definition
├── pytest.ini
├── requirements.txt
├── .env.example
├── docs/
│   ├── SETUP.md
│   └── STRATEGIES.md
├── bot/
│   ├── config.py                    # typed env-driven config
│   ├── logger.py                    # shared Rich console
│   ├── models.py                    # Opportunity, Leg, Fill, priorities
│   ├── queue.py                     # priority heap + dedup
│   ├── risk.py                      # RiskManager (circuit breakers)
│   ├── state.py                     # JSON + Supabase persistence
│   ├── scanner.py                   # 5-second market scanner
│   ├── executor.py                  # order dispatcher
│   ├── dashboard.py                 # Rich live dashboard
│   ├── clients/
│   │   ├── rpc.py                   # Polygon RPC with failover
│   │   ├── polymarket.py            # Gamma + Data + CLOB wrappers
│   │   ├── telegram.py              # alerts + kill-switch listener
│   │   ├── binance.py               # CEX confirmation for DipArb
│   │   └── supabase_client.py       # optional fill mirror
│   ├── strategies/
│   │   ├── base.py
│   │   ├── arbitrage.py
│   │   ├── tail_end.py
│   │   ├── micro_spread.py
│   │   ├── dip_arb.py
│   │   ├── smart_copy.py
│   │   ├── market_making.py
│   │   └── sniper.py
│   └── backtest/
│       └── engine.py                # historical backtest harness
└── tests/
    ├── conftest.py
    ├── test_risk.py                 # 9 tests
    ├── test_queue.py                # 4 tests
    ├── test_arbitrage.py            # 4 tests
    ├── test_tail_end.py             # 5 tests
    ├── test_smart_copy_scoring.py   # 4 tests
    └── test_backtest.py             # 1 test
```

---

## Safety

* `.env` is git-ignored and must never be committed.
* The bot refuses to run in `MODE=live` without a private key and
  funder address.
* The risk manager is the only path to the executor — every opportunity
  is gated on exposure, loss caps, drawdown, and pause state.
* The Telegram `/emergencystop` flips a kill switch that blocks **all**
  new orders instantly. Already-resting GTC orders are untouched; use
  the Polymarket UI or an operator script to cancel them if needed.
* This is a personal-use tool. It is **not** financial advice and
  **not** audited. Run it in paper mode first, expect bugs, never
  risk more than you can afford to lose.

---

## Running the tests

```bash
pytest -q
# 27 passed
```

---

## Further reading and references

Architecture and API patterns informed by:

* <https://github.com/HKUDS/Vibe-Trading> — backtesting framework, risk
  management, multi-strategy orchestration.
* <https://github.com/GiordanoSouza/polymarket-copy-trading-bot> —
  Supabase realtime, position sizing.
* <https://github.com/direkturcrypto/polymarket-terminal> — maker-rebate
  MM, ghost-fill recovery, WebSocket RTDS.
* [Polymarket docs](https://docs.polymarket.com), especially the
  [CLOB quickstart](https://docs.polymarket.com/trading/quickstart) and
  [WebSocket overview](https://docs.polymarket.com/market-data/websocket/overview).
