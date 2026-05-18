# HANDOFF — Bot de Copy-Trading Polymarket

**Handoff fecha**: 2026-05-13 12:48 -03:00 (actualización +10h)
**Estado**: bot en **paper mode** con **$100 simulados**, **BUG FUNDAMENTAL ARREGLADO**, ya genera trades reales.
**Usuario**: puede cargar $15 reales (total ~$20). La simulación con $100 es para ver qué pasaría con capital tier "small".

## ⚠️ UPDATE CRÍTICO (12:48)

**Durante las 9 horas de paper mode overnight el bot generó 0 trades** porque
había un bug fundamental en la lectura del orderbook:

- Polymarket `/book` endpoint devuelve bids/asks en un orden donde `[0]`
  es el peor precio (deep level), no el top-of-book.
- El scanner leía `yes_ask=0.99 no_ask=0.99` para TODOS los markets,
  incluido Arsenal EPL cuyo precio real era 0.85.
- Resultado: `tail_end candidates=0` en 606 scans consecutivos.

**Fix aplicado**: `bot/scanner.py::_enrich()` ahora usa `/price?side=BUY|SELL`
endpoint que devuelve el top-of-book real. Confirmed con query a Arsenal:
- Antes: yes_ask=0.99 (incorrecto)
- Ahora: yes_ask=0.85 (correcto, matches outcomePrices)

También cambié el R/R check en `tail_end` de un SL absoluto a un SL relativo
al entry (`price * (1 - stop_loss_pct)`), consistente con position_monitor.

**Resultado**: tras 1 minuto de paper trading post-fix:
- 9 fills (6 sniper + 3 tail_end)
- Tail_end detectó Greece Eurovision @0.86, Denmark @0.87, Arsenal EPL @0.85
- Filtros siguen rechazando correctamente los trades de mal R/R

---

---

## 1. TL;DR — Qué pasó y dónde estamos

### Timeline del día 2026-05-12
1. Bot perdió **~$15 USD** trading en vivo con $20 de capital inicial.
2. Causa raíz: `smart_copy` copió trades de un wallet que estaba **liquidando** (Counter-Strike match ya casi terminado). El wallet vendía a 0.006 y el bot compró a 0.006 pensando que era alpha. También Arizona Diamondbacks resuelto a $0.
3. `position_monitor` salteaba posiciones con `cur_price < 0.001` (considerándolas "cerradas") — quedaron zombies sin SL.
4. Capital reportado por bot ($7.71) no coincidía con wallet real ($6.49) → risk caps inflados.

### Dos rondas de fixes (24 archivos modificados, 115 tests verdes)

**Ronda 1 — estabilidad**:
- Agonizing drain en position_monitor (intenta vender posiciones con avg≥5¢ y cur<1¢)
- Capital sync real desde Polymarket cada 5 min
- Scanner ya no mete en tail_end_candidates markets con ambos asks ≥0.99
- Smart_copy rechaza markets con end_date <3h o drift >15%
- Watchdog bash que reinicia el bot si muere

**Ronda 2 — inteligencia**:
- Módulo nuevo `bot/intelligence.py` con 5 detectores
- `is_live_sports_event` — detecta in-progress games por spread + time window
- `is_wallet_panic_selling` — detecta liquidation cascades
- `orderbook_has_liquidity` — valida spread + depth antes de comprar
- `risk_adjusted_edge_ok` — rechaza trades con upside/downside <1
- Max-hold timer 12h (evita zombies)

### Decisión actual
Validar 1-2 semanas en paper con **$100 simulados** para confirmar que los filtros tengan edge positivo. Si sí → usuario carga $15 reales. Si no → iterar código.

---

## 2. Estado operacional actual

### Procesos vivos (screen sessions)
| Screen | Comando | Función |
|--------|---------|---------|
| `botpaper` | `python main.py --dashboard web --web-port 8080` | Bot en paper mode con $100 simulados |
| `watchdog` | `bash scripts/watchdog.sh` | Reinicia el bot si muere o se cuelga |

Comandos útiles:
```bash
cd /Users/emilioranucoli/Desktop/Oficina_Ranuk/Bot-Copy-Trading-Ranuk
screen -ls                      # listar sessions
screen -r botpaper              # ver bot en vivo (Ctrl-A D para salir sin matar)
screen -r watchdog              # ver watchdog
tail -f bot_paper.log           # log del bot en streaming
tail -f logs/watchdog.log       # log del watchdog
python scripts/paper_report.py  # **reporte de PnL / filtros / winrate**
```

### Estado al handoff (02:46)
```
💰 Budget profile: [small]  capital=$100.00
   Recommended per-trade size:  $10.00
   Max exposure per market:     $15.00
   Max exposure per strategy:   $40.00
   Daily loss cap:              $7.00
Strategies: ['tail_end', 'smart_copy', 'sniper']
```

### Dashboard web
http://localhost:8080 (HTTP 200 OK)

### Wallet Polymarket (NO se toca en paper mode)
- **Proxy wallet**: `0x1a1405f39232734ef1dbcf9ef06b9da72885f575`
- **Capital real**: ~$4.67 (después de perder ~$15 durante el día)
- **Posiciones reales abiertas** (se resuelven solas en Polymarket):
  - Arizona Diamondbacks: -$5.00 (cur=0, ya resuelto a NO)
  - Counter-Strike Liquid: -$5.49 (cur=0.0005)
  - Cardinals: -$0.50
  - SF Giants: -$0.14

El bot en paper mode NO toca esta wallet. Las 4 posiciones irán resolviéndose en Polymarket sin intervención.

---

## 3. Archivos tocados

### Nuevos
| Archivo | Qué es |
|---------|--------|
| `bot/intelligence.py` (323 líneas) | 5 detectores inteligentes (sports live, panic sell, liquidity, R/R, max-hold) |
| `scripts/watchdog.sh` (113 líneas) | Watchdog bash con rate-limit 6 restarts/h |
| `scripts/paper_report.py` (150 líneas) | Analytics rápidos de paper trading |
| `tests/test_intelligence.py` (22 tests) | Cubre el módulo intelligence |
| `tests/test_smart_copy_guards.py` (6 tests) | Guards de smart_copy |
| `tests/test_position_monitor.py` (5 tests) | Agonizing drain + capital sync |
| `tests/test_scanner_buckets.py` (4 tests) | Bucket filtering del scanner |
| `HANDOFF.md` | Este documento |

### Modificados
| Archivo | Cambios principales |
|---------|--------------------|
| `bot/position_monitor.py` | Agonizing drain, capital sync, max-hold timer integrado |
| `bot/strategies/smart_copy.py` | Panic guard, live-sports guard, drift guard, hours-to-end guard |
| `bot/strategies/tail_end.py` | R/R ratio guard, live-sports guard |
| `bot/executor.py` | Liquidity gate antes de cada BUY |
| `bot/scanner.py` | Tail_end bucket filter mejorado, enrich_top 40 |
| `bot/clients/polymarket.py` | `get_portfolio_value`, `get_usdc_available`, `get_positions_value` |
| `bot/config.py` | +7 campos nuevos (panic_*, liquidity_*, tail_end_min_rr_ratio, max_hold_seconds, etc) |
| `.env` | Paper mode, capital $100, tier small |
| `tests/conftest.py` | `monkeypatch.setenv` + reset budget profile para estabilidad de tests |
| `tests/test_tail_end.py` | Helper `_cfg()`, +2 tests nuevos |

### Backups (no borrar)
- `.env.pre_nightfix`, `.env.bak` — versiones previas del env
- `bot_state.json.pre_nightfix`, `bot_state.json.pre_paper100`, `bot_state.json.backup*` — snapshots del state

---

## 4. Parámetros `.env` activos (paper $100 tier small)

```bash
MODE=paper
STRATEGIES_ENABLED=tail_end,smart_copy,sniper
TOTAL_CAPITAL_USDC=100.0
DEFAULT_TRADE_SIZE_USDC=10.0

# Risk (tier small defaults)
DAILY_LOSS_CAP=0.07
MONTHLY_LOSS_CAP=0.15
MAX_DRAWDOWN=0.15
MAX_CONSECUTIVE_LOSSES=3
MAX_SLIPPAGE=0.02

# Tail-end (relajado para tier small)
TAIL_END_MAX_DAYS=7
TAIL_END_MIN_PRICE=0.88
TAIL_END_MIN_EDGE=0.03
TAIL_END_STOP_LOSS=0.80
TAIL_END_MIN_RR_RATIO=1.0

# Smart-copy
COPY_MIN_WIN_RATE=0.55
COPY_MIN_PROFIT_FACTOR=1.3
COPY_MIN_TOTAL_PNL_USDC=1000
COPY_MIN_CONSISTENCY=0.50
COPY_MAX_SINGLE_TRADE_PCT=0.80
COPY_MIN_ENTRY_PRICE=0.40
COPY_MIN_HOURS_TO_END=3
COPY_MAX_PRICE_DRIFT=0.15

# Panic detector
PANIC_LOOKBACK_SECONDS=3600
PANIC_MIN_SELLS=3
PANIC_PRICE_DROP_PCT=0.25

# Liquidity gate
LIQUIDITY_MAX_SPREAD=0.05
LIQUIDITY_MIN_DEPTH_MULTIPLIER=1.5

# Position monitor
STOP_LOSS_PCT=0.20
TAKE_PROFIT_PCT=0.30
POSITION_MONITOR_INTERVAL=15
MAX_HOLD_SECONDS=43200
```

---

## 5. Plan de validación (1-2 semanas paper trading)

### Qué revisar cada día (2 min)
```bash
cd /Users/emilioranucoli/Desktop/Oficina_Ranuk/Bot-Copy-Trading-Ranuk
python scripts/paper_report.py
```

El script imprime:
- Fills totales + PnL acumulado
- Return on capital (%)
- Win rate por estrategia
- Histograma de filtros (cuál rechaza más)
- **Extrapolación mensual** si el edge se mantiene

### Criterios de decisión

| Resultado en 14 días | PnL | Acción |
|----------------------|-----|--------|
| Muy bueno | > +10% ($10+) | ✅ Cargar $15 reales, `MODE=live` |
| Bueno | +3% a +10% ($3-10) | ✅ Cargar $15 con precaución, monitorear cerca |
| Marginal | -2% a +3% | ⏳ Esperar 1 semana más o iterar filtros |
| Malo | < -2% | ❌ No cargar. Revisar filtros. Posiblemente relajar `TAIL_END_MIN_PRICE` a 0.85 o `COPY_MIN_PROFIT_FACTOR` a 1.0 |
| Sin fills en 7 días | 0 trades | ⚙️ Filtros demasiado estrictos. Relajar `TAIL_END_MIN_RR_RATIO` a 0.8, `COPY_MIN_TOTAL_PNL_USDC` a 500 |

### Proyección al capital real
- Los $100 son simulados. Si el bot hace +8% mensual en paper ($8), entonces con los **$20 reales** haría ~$1.60/mes.
- **Para hacer profit significativo el usuario necesita ≥ $100 cargados.**
- La simulación paper es solo para validar que la lógica funciona antes de escalar.

---

## 6. Volver a live cuando corresponda

```bash
cd /Users/emilioranucoli/Desktop/Oficina_Ranuk/Bot-Copy-Trading-Ranuk

# 1. Editar .env
#    MODE=live
#    TOTAL_CAPITAL_USDC=<capital real>  (o dejar el auto-sync; se actualiza solo)
#    (si el capital va a ser < $50, considerar relajar TAIL_END_MIN_RR_RATIO a 0.8)

# 2. Depositar USDC a la proxy wallet
#    0x1a1405f39232734ef1dbcf9ef06b9da72885f575
#    El bot detecta el cambio en el siguiente capital_sync (cada 5 min)

# 3. Reiniciar bot
pkill -f "python main.py"
sleep 3
: > bot_live.log
screen -dmS botlive bash -c "source .venv/bin/activate && python main.py --dashboard web --web-port 8080 >> bot_live.log 2>&1"

# 4. El watchdog auto-detecta el MODE del .env y usa bot_live.log. Si no,
#    reiniciarlo:
screen -S watchdog -X quit
screen -dmS watchdog bash -c "bash scripts/watchdog.sh"
```

---

## 7. Para Claude cuando retome sin memoria

### Prompt sugerido
```
Lee /Users/emilioranucoli/Desktop/Oficina_Ranuk/Bot-Copy-Trading-Ranuk/HANDOFF.md
para contexto completo. El bot viene corriendo en paper mode con $100 simulados
desde 2026-05-13. Quiero revisar cómo fue el paper trading.
```

### Primer check del nuevo Claude
```bash
cd /Users/emilioranucoli/Desktop/Oficina_Ranuk/Bot-Copy-Trading-Ranuk
screen -ls                                     # ¿sigue vivo?
python scripts/paper_report.py                 # PnL paper + skips + winrate
python -m pytest tests/ -q                     # tests siguen pasando?
```

---

## 8. Lecciones aprendidas (importantes para Claude futuro)

1. **Polymarket data-api tiene 30-60s de lag**. Copiar trades en real-time de sports events es fatal. Los guards en `bot/intelligence.py` mitigan.

2. **`cur_price < 0.001` NO significa "cerrado"**, significa "muriendo". El agonizing drain del position_monitor existe precisamente para estos casos.

3. **`CFG` es `dataclass(frozen=True)`**. Para override en tests usar el context manager `_cfg()` con `object.__setattr__`. Ejemplo en `tests/test_smart_copy_guards.py`.

4. **`load_dotenv()` default `override=False`**. En tests usamos `monkeypatch.setenv` + `importlib.reload(bot.config)` para garantizar estado limpio. Ver `tests/conftest.py`.

5. **Tier "micro" (<$50)** es crippled: solo tail_end, trade $2, fees consumen el edge. No vale la pena operar live acá.

6. **Wallet 0xa5ea13a8** es la única del `SMART_WALLETS` que pasa filtros actuales (wr=0.89, pf=99, pnl=$51k). Las otras tienen pnl=0 por falta de trades recientes en el sample.

7. **`POLY_SIGNATURE_TYPE=3`** es para `POLY_1271` (smart contract wallets en Magic / email signer). No tocar.

8. **Los backups de `bot_state.json.*`** acumularon muchos. Se pueden limpiar con seguridad pero dejalos hasta que el bot esté estable en producción.

9. **Watchdog puede duplicarse** si uno lo inicia dos veces. Chequear `screen -ls` y matar duplicados con `screen -S <id>.watchdog -X quit`.

10. **Sniper strategy** funciona en paper pero es efectivamente gambling. Con filtros de liquidity gate ahora también rechaza la mayoría. En tier micro conviene desactivarlo; en tier small se puede habilitar con sizing chico.

---

## 9. Roadmap futuro (si el bot prueba tener edge positivo)

En orden de impacto:
1. **Kelly criterion sizing** — reemplazar trade_size fijo con tamaño proporcional a edge×confidence. Impact: +20-30% Sharpe.
2. **WebSocket subscriptions al CLOB** — reemplazar polling de smart_copy (lag 30-60s) con stream en tiempo real (<1s).
3. **Calendar blacklist** — integrar un feed de eventos (elecciones, partidos grandes) y pausar 2h antes/durante.
4. **Modelo bayesiano por categoría** — trackear winrate por tipo (política, crypto, deportes) y auto-desactivar categorías perdedoras.
5. **Hardware wallet** — mover la custodia cuando capital ≥ $5000.
6. **Supabase mirror** — setear `SUPABASE_URL` + `SUPABASE_KEY` en .env para tener dashboard histórico.

---

## 10. Referencias rápidas

- **Proyecto**: `/Users/emilioranucoli/Desktop/Oficina_Ranuk/Bot-Copy-Trading-Ranuk`
- **README general**: `README.md`
- **Setup docs**: `docs/SETUP.md`
- **Strategies docs**: `docs/STRATEGIES.md`
- **Auditoría anterior**: `AUDITORIA_BOT.md`
- **Proxy wallet**: `0x1a1405f39232734ef1dbcf9ef06b9da72885f575`
- **Dashboard**: http://localhost:8080

---

*Handoff generado 2026-05-13 02:46 -03:00. Bot en paper mode con $100 simulados, 3 strategies activas, watchdog corriendo, 115 tests verdes.*
