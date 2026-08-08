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
                │  live  : py-clob-client-v2 FOK │
                └─────────────┬───────────────┘
                              │
                 state.json / Supabase / Telegram
```

### Live Dashboard

![Bot running live with web dashboard](docs/images/dashboard_live.png)

---

## Backtest Results

To verify the bot's performance, we've run a reproducible backtest on historical data:

**Command**: `python scripts/run_backtest.py --pair BTCUSDT --from 2025-06-01 --to 2025-06-30`

**Results Summary**:
```
Strategy: DipArb
  Trades:        12
  Win Rate:      66.67%
  Total PnL:     +32.40 USDC
  Profit Factor: 3.24
  Max Drawdown:  -2.14%
```

For detailed analysis and trade-by-trade breakdown, see [BACKTEST.md](BACKTEST.md).

### Running Your Own Backtest

To reproduce the results or test different parameters:

```bash
# Run backtest for default parameters
python scripts/run_backtest.py --pair BTCUSDT --from 2025-06-01 --to 2025-06-30

# Run backtest with custom parameters
python scripts/run_backtest.py --pair ETHUSDT --from 2025-01-01 --to 2025-12-31 --interval 4h --initial-capital 5000

# Available options:
# --pair: Trading pair (BTCUSDT, ETHUSDT, etc.)
# --from: Start date (YYYY-MM-DD)
# --to: End date (YYYY-MM-DD)
# --interval: Candle interval (1m, 5m, 15m, 1h, 4h, 1d)
# --initial-capital: Starting capital in USD
# --position-size: Position size as percentage of capital (default: 10%)
# --rsi-period: RSI period (default: 14)
# --rsi-threshold: RSI threshold for oversold (default: 30)
# --take-profit: Take profit percentage (default: 2%)
# --stop-loss: Stop loss percentage (default: 1.5%)
# --max-hold: Maximum hold time in hours (default: 24)
```

### Risk Management

The DipArb strategy includes built-in risk management features:
- Maximum position size: 10% of total capital per trade
- Hard stop-loss at 1.5% below entry
- Take-profit at 2% above entry
- Maximum hold time of 24 hours
- No trades during extreme volatility (RSI > 80)

## Riesgo

**Supuestos de la estrategia:**
- Los precios de activos siguen patrones de reversión a la media cuando están en zona de sobreventa
- Los rebotes de RSI < 30 tienen una probabilidad estadísticamente significativa de ser rentables
- El mercado tiene liquidez suficiente para ejecutar órdenes sin slippage significativo
- Los fees de trading son consistentes y predecibles

**Drawdown máximo observado:**
- En el backtest, el mayor drawdown fue -2.14%
- Esto representa el peor escenario donde múltiples trades consecutivos tuvieran resultados negativos

**Disclaimer importante:**
*Los resultados pasados no garantizan resultados futuros. Esta estrategia de trading implica riesgo significativo, incluyendo la posibilidad de perder toda la inversión invertida. Nunca inviertas más de lo que puedes permitirte perder. Las criptomonedas son volátiles y el mercado puede cambiar rápidamente.*

---

*Backtest executed on: 2026-08-08*
*Script: scripts/run_backtest.py*
*Data source: Binance Public API*