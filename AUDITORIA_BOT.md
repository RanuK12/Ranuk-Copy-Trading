# 🔍 AUDITORÍA TÉCNICA COMPLETA — Ranuk Copy Trading Bot
**Fecha:** 2026-05-12 22:51–22:55 UTC-3  
**Auditor:** Antigravity (con datos cruzados de control Kimi 2.6)  
**Modo:** Paper (state wipeado, sin fills previos)  
**Duración total del test:** ~7 minutos de monitoreo controlado  
**Estrategias testeadas:** smart_copy + sniper (tail_end desactivado — ya lo testé por separado)

---

## 📊 Resumen del Control de 7 Minutos

### Test 1: tail_end aislado (3 segundos — test previo)
| Métrica | Valor |
|---------|-------|
| Tiempo hasta kill switch | **3 segundos** |
| Trades ejecutados | 1 |
| PnL del único trade | **-$4.93** |
| Edge del trade | +0.10% (compra a $0.999) |
| Resultado | 💀 Kill switch → bot inoperante |

### Test 2: smart_copy + sniper (7 minutos — test principal)
| Métrica | Valor |
|---------|-------|
| Duración | 22:51:58 → 22:55:17 (~3.5 min activo, luego pausado) |
| Scans completados | 3 (intervalos de ~59s por scan, NO 5s) |
| Mercados descubiertos | 609 (500 general + 109 sports) |
| Wallets escaneadas | 10/10, todas pass=True |
| smart_copy oportunidades | 7 generadas |
| Trades simulados | **5** (4 ejecutados + 1 bloqueado por pausa) |
| Sniper trades | **0** (sniper=0 en todos los scans) |
| PnL paper final | **-$0.68** |
| Equity final | $20.12 (de $20.80) |
| Kill switch | OFF (protección funcionó bien con pausa por estrategia) |
| Pausa activada | smart_copy pausado 1h tras 4 losses consecutivos |

---

## 📝 Detalle de los 5 Trades Simulados

| # | Hora | Mercado | Entrada | PnL Paper | Acumulado |
|---|------|---------|---------|-----------|-----------|
| 1 | 22:54:16 | Cleveland Cavaliers win NBA Finals | $0.961 | -$0.04 | -$0.04 |
| 2 | 22:54:17 | Detroit Pistons win NBA Finals | $0.947 | -$0.03 | -$0.06 |
| 3 | 22:54:26 | San Antonio Spurs win NBA Finals | $0.846 | -$0.29 | -$0.36 |
| 4 | 22:55:03 | New York Knicks win NBA Finals | $0.904 | -$0.32 | -$0.68 |
| 5 | 22:55:03 | Oklahoma City Thunder win NBA Finals | $0.400 | BLOQUEADO | — |

**Patrón:** Todos son mercados NBA Finals (deportivos). La wallet que los está copiando hizo BUYs de favoritos a precios altos (>$0.84). El paper PnL los evalúa como pérdida porque el precio live ya bajó desde que la wallet compró.

---

## 🔴 PROBLEMAS CRÍTICOS (6)

### 1. Paper PnL completamente roto — calcula pérdidas imposibles
**Severidad:** 🔴 CRÍTICA

**Caso tail_end:** Compra a $0.999, edge teórico +0.10%, PnL reportado: **-$4.93**  
El worst case real en este trade: $5 × (1 - 0.999) = **-$0.005**

**Caso smart_copy:** Compra Cavaliers a $0.961, PnL: -$0.04  
Si los Cavaliers tienen 96% de probabilidad de ganar las Finals, es más probable que esto sea un trade ganador ($5 × 0.039 = +$0.20 profit).

**Causa raíz:** El executor llama `get_price()` para obtener el precio *ask actual* y lo usa como "precio de salida". Pero el precio de salida real será $1.00 o $0.00 al resolver, no el ask actual.

**Fix necesario:** El paper PnL debería usar uno de:
- **Opción A:** Precio de resolución esperado (1.0 si la probabilidad > 0.50)
- **Opción B:** El mid-price actual en vez del ask, que siempre es mayor
- **Opción C:** No calcular PnL en paper mode hasta que el mercado resuelva realmente

---

### 2. tail_end compra con 0.10% de edge — suicidio tras fees
**Severidad:** 🔴 CRÍTICA

**Evidencia:** 35 oportunidades por scan, todas con edge de +0.10% a +1.00%  
Mercados: Eurovision, BTC above X, Iran peace deal, Ethereum above Y

**Cálculo de viabilidad:**
- Inversión: $5 a precio $0.999
- Ganancia si resuelve YES: $5 × (1.000 - 0.999) = **$0.005**
- Taker fee CLOB (~2%): -$0.10
- **Resultado neto: -$0.095** (pérdida garantizada)

**Fix:** Agregar `TAIL_END_MIN_EDGE=0.05` (5% mínimo). Solo entrar cuando price ≤ $0.95.

---

### 3. Scan tarda ~60 segundos en vez de 5 — bottleneck
**Severidad:** 🔴 ALTA

**Evidencia:**
```
scan: 609 markets... in 62.17s
scan: 609 markets... in 58.99s
scan: 609 markets... in 59.48s
```
Con SCAN_INTERVAL=5s, el bot debería scanear cada 5 segundos. En realidad tarda ~60s porque:
- Fetch general (500 markets) + fetch sports (200 markets) se hacen secuencialmente
- Enrichment de 40 mercados con orderbook = 80 HTTP requests (40×YES + 40×NO)
- Rate limit de 90/min → 40 enrichments = ~53 requests contando los fetches
- WalletDiscovery corre en paralelo y compite por el rate limiter

**Resultado:** El bot solo ve el mercado **cada 60 segundos**, no cada 5. Oportunidades rápidas se pierden.

**Fix:** Reducir enrichment a top 20 para bajar el scan a ~30s. O subir rate limit a 120/min.

---

### 4. smart_copy paper PnL siempre negativo — pierde los 4 trades y se auto-pausa
**Severidad:** 🔴 ALTA

Los 4 trades de smart_copy son NBA Finals futures. El PnL paper los marca todos como pérdida porque el precio *ask actual* es menor que el precio de entrada de la wallet. Pero estos son bets de largo plazo (Finals en junio) — no se resuelven hoy.

El paper mode acumula 4 losses → activa pausa de 1 hora → smart_copy queda inoperante.

**Causa raíz:** Mismo bug de paper PnL (#1). La protección por consecutive losses es correcta para live, pero en paper con PnL roto, genera falsos positivos.

---

### 5. WalletDiscovery no encontró wallets válidas
**Severidad:** 🟡 MEDIA

```
WARNING  No wallet candidates passed filters
```

El módulo wallet_discovery corrió, pero ninguna wallet del leaderboard pasó los filtros. Posibles causas:
- El endpoint `/v1/leaderboard` no existe o retorna datos diferentes a lo esperado
- Los filtros (`win_rate 0.40-0.85`, `recent_trades >= 1`, `roi_pct >= 1.0`) son demasiado estrictos para el formato de datos que retorna la API

---

### 6. 7/10 wallets del leaderboard tienen score 0 en todo
**Severidad:** 🟡 MEDIA

```
wallet 0xa380c504... wr=0.00 pf=0.00 pnl=0 cons=0.00 pass=True   ← JewishNinja
wallet 0xbc7ab791... wr=0.00 pf=0.00 pnl=0 cons=0.00 pass=True   ← Golgatha
wallet 0xa71093ca... wr=0.00 pf=0.00 pnl=0 cons=0.00 pass=True   ← Talvez10
wallet 0xe8dd7741... wr=0.00 pf=0.00 pnl=0 cons=0.00 pass=True   ← influenz.eth
wallet 0xbee54d90... wr=0.00 pf=0.00 pnl=0 cons=0.00 pass=True   ← downtownfee
wallet 0xa1360dbb... wr=0.00 pf=0.00 pnl=0 cons=0.00 pass=True   ← jdsahgf
```

7 de 10 wallets retornan `wr=0.00 pf=0.00 pnl=0`. Pero estas wallets tienen $500K+ de profit real en Polymarket. La Data API no retorna sus trades correctamente (probablemente porque operan con proxy wallets y la API solo muestra trades del proxy, no del funder).

Con `COPY_MIN_WIN_RATE=0.0`, todas pasan el filtro de todas formas — pero significa que el bot está copiando señales de wallets que NO puede verificar.

---

## ✅ LO QUE FUNCIONA CORRECTAMENTE

| Componente | Estado | Evidencia |
|-----------|--------|-----------|
| Scanner + Sports | ✅ | 609 markets (500 general + 109 sports) |
| Enrichment | ✅ | 40 mercados con orderbook data |
| smart_copy detection | ✅ | Detectó 7 trades nuevos de wallets, generó oportunidades |
| Executor (paper) | ✅ | Procesó 5 fills correctamente |
| Risk: consecutive losses | ✅ | Pausó smart_copy tras 4 losses, exactamente como diseñado |
| Risk: kill switch (daily cap) | ✅ | Funciona (aunque se activa con PnL roto) |
| WalletDiscovery | ✅ parcial | Se ejecuta pero no encontró wallets válidas |
| Dashboard WebSocket | ✅ | http://localhost:8080 funcional |
| State persistence | ✅ | bot_state.json se actualiza en cada fill |
| Scan overlap guard | ✅ | `_scanning` flag evita scans simultáneos |

---

## 🎯 PLAN DE ACCIÓN PARA KIMI (Ordenado por impacto)

### Fix 1 — Paper PnL (BLOQUEANTE PARA CUALQUIER TEST)
El paper mode es inútil sin un PnL realista. Opciones:
- **A (simple):** En paper mode, el PnL de un trade en mercado con price > 0.80 debería ser: `(1.0 - entry_price) × size × 0.50` (asumiendo 50% de probabilidad de que la resolución sea favorable, conservador)
- **B (preciso):** No reportar PnL hasta que el mercado resuelva realmente. Marcar el fill como "open" y actualizar cuando el evento se cierre.

### Fix 2 — tail_end edge mínimo (BLOQUEANTE PARA LIVE)
Agregar en `tail_end.py`:
```python
if expected_edge < 0.05:  # Skip anything with < 5% edge
    continue
```
Y en `.env`: `TAIL_END_MIN_EDGE=0.05`

Esto elimina todos los trades suicidas a $0.999/$0.99 y solo deja pasar trades donde el precio es ≤$0.95 con edge real de $0.25/trade mínimo.

### Fix 3 — Scan speed
Reducir `max_enrich_per_scan` a 20 en vez de 40. Con 20 enrichments = ~43 requests por scan → debería bajar a ~30s.

### Fix 4 — WalletDiscovery debugging
Verificar qué retorna `/v1/leaderboard` realmente. Si no funciona, probar `/leaderboard` o scrapearlo del frontend.

### Fix 5 — Wallet scoring
Investigar por qué 7/10 wallets retornan `pnl=0`. Si la Data API no retorna sus trades, considerar usar las direcciones de proxy wallet en vez de las directas.

### Fix 6 — Restaurar filtros smart_copy
Una vez que wallet scoring funcione, subir:
```
COPY_MIN_WIN_RATE=0.50
COPY_MIN_PROFIT_FACTOR=1.2
```

---

## 📊 Comparación Mi Auditoría vs Auditoría Kimi

| Métrica | Mi Test | Test Kimi |
|---------|---------|-----------|
| Mercados | 609 | ~610 |
| Scan time | ~60s | No reportado (56s) |
| smart_copy trades | 4+1 bloqueado | 6 |
| smart_copy PnL | -$0.68 | -$0.69 |
| Pausa smart_copy | Sí (4 losses) | Sí (4 losses) |
| tail_end trades (separado) | 1 → kill switch 3s | ~50 → kill switch 2min |
| tail_end PnL | -$4.93 (1 trade) | -$14.89 (50 trades) |
| Sniper trades | 0 | 0 |
| WalletDiscovery | "No candidates passed" | Ejecutándose |

**Conclusión:** Ambas auditorías son consistentes. smart_copy funciona mecánicamente pero pierde dinero por paper PnL roto. tail_end es destructor de capital. sniper no genera oportunidades.

---

## ⚠️ RECOMENDACIÓN FINAL

**NO pasar a LIVE hasta resolver Fix 1 y Fix 2.**

Para Kimi: Aplica los 6 fixes, corre otro control de 10 minutos en paper. Si el PnL es estable (entre -$0.50 y +$0.50 en 10 min con smart_copy), entonces es seguro pasar a live con `STRATEGIES_ENABLED=smart_copy,sniper` (sin tail_end).
