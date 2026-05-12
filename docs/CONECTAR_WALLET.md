# Guía: Conectar el bot a tu cuenta de Polymarket

Esta es la guía más importante antes de operar en vivo. Si te equivocás en
estos pasos, podés perder dinero real. Leéla entera antes de tocar nada.

> **Regla de oro:** empezá siempre en `MODE=paper` durante 24-48 horas
> antes de pasar a `live`. El bot simula toda la lógica sin tocar la
> blockchain y es la única forma de validar que el vínculo con tu
> cuenta quedó bien antes de arriesgar fondos.

---

## Índice

1. [Cómo funciona Polymarket (wallets proxy)](#1-como-funciona-polymarket-wallets-proxy)
2. [Qué tipo de cuenta tenés](#2-que-tipo-de-cuenta-tenes)
3. [Paso A — Obtener tu Funder Address](#3-paso-a--obtener-tu-funder-address-proxy-wallet)
4. [Paso B — Obtener tu Private Key](#4-paso-b--obtener-tu-private-key)
5. [Paso C — Depositar USDC](#5-paso-c--depositar-usdc-en-la-red-polygon)
6. [Paso D — Aprobar contratos (solo EOA)](#6-paso-d--aprobar-contratos-solo-wallet-externa)
7. [Paso E — Configurar `.env`](#7-paso-e--configurar-env)
8. [Paso F — Validación paper antes de live](#8-paso-f--validacion-paper-antes-de-live)
9. [Cómo pasar a live](#9-como-pasar-a-live)
10. [Checklist de seguridad](#10-checklist-de-seguridad-antes-de-live)
11. [Errores comunes](#11-errores-comunes-y-como-evitarlos)
12. [Preguntas frecuentes](#12-preguntas-frecuentes)

---

## 1. Cómo funciona Polymarket (wallets proxy)

Polymarket **no** es un exchange tradicional como Binance. Tiene un modelo
de wallets proxy que confunde a todo el mundo al principio. Dos direcciones
distintas participan en cada trade:

| Dirección | Qué hace | De dónde sale |
|---|---|---|
| **EOA (Externally Owned Account)** | **Firma** las órdenes con tu clave privada | Es tu wallet de MetaMask/Rabby, o la que Polymarket te crea con email |
| **Funder / Proxy Wallet** | **Tiene los fondos** (USDC, posiciones) | Polymarket te la genera automáticamente al crear cuenta |

Cuando el bot quiere operar:

1. Firma una orden con la EOA (`POLY_PRIVATE_KEY`).
2. Polymarket verifica la firma y ejecuta la orden **usando los fondos del
   Funder** (`POLY_FUNDER`).

Por eso en `.env` hay dos direcciones. Si las confundís o mandás USDC a la
EOA en vez del Funder, el bot no ve los fondos.

---

## 2. Qué tipo de cuenta tenés

Elegí **una** de estas dos opciones antes de seguir. Condiciona todos los
pasos siguientes.

### Opción A — Cuenta con email (Magic.link) — **recomendada para empezar**

Usás la cuenta que creaste en polymarket.com con email o Google. Polymarket
la maneja internamente con Magic.link.

**Ventajas:**
- No hay que aprobar contratos manualmente — Polymarket lo hace solo.
- El Funder se crea automáticamente.
- Menos pasos, menos margen de error.

**Desventajas:**
- Tenés que exportar la private key desde Polymarket (flujo deliberadamente
  fricatoso para prevenir phishing).
- Estás confiando en Magic para la custodia.

**Usá `POLY_SIGNATURE_TYPE=1` en el `.env`.**

### Opción B — Wallet externa (MetaMask, Rabby)

Conectaste tu wallet externa a polymarket.com. Vos controlás la clave
privada desde el día cero.

**Ventajas:**
- Custodia propia total.
- Podés mover fondos sin depender de Polymarket.

**Desventajas:**
- Hay que aprobar 3 contratos (allowances) antes de operar.
- **Hardware wallets (Ledger, Trezor) NO sirven** para el bot, porque el
  bot necesita firmar órdenes programáticamente y las hardware wallets
  por diseño no permiten exportar la clave. Tenés que usar una hot
  wallet (MetaMask/Rabby) **dedicada solo al bot**.

**Usá `POLY_SIGNATURE_TYPE=0` en el `.env`.**

### Mi recomendación

1. Empezá con **Opción A** en `MODE=paper` durante una semana.
2. Si todo funciona y querés custodia propia, creá una MetaMask nueva,
   transferí fondos chicos desde tu cuenta Magic y migrá a **Opción B**
   para live.

---

## 3. Paso A — Obtener tu Funder Address (Proxy Wallet)

Este paso es **igual para ambas opciones**.

1. Entrá a <https://polymarket.com> y logueate.
2. Hacé click en tu avatar (arriba a la derecha) → "Profile" (tu perfil público).
3. Mirá la URL del navegador: `https://polymarket.com/profile/0xABC123...`
   **Esa dirección `0xABC...` es tu `POLY_FUNDER`.** Copiala.
4. Alternativa: Settings → Wallet → "Deposit Address" o "Polymarket Wallet
   Address" muestra la misma dirección.

Guardala. Ya tenés la mitad de lo que necesita el bot.

### Verificá que la dirección es correcta

Pegala en <https://polygonscan.com/address/0x...>. Deberías ver:

- Alguna actividad histórica si ya operaste.
- Transacciones con el contrato CTF Exchange
  (`0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`).

Si la dirección está vacía sin ninguna transacción, probablemente
copiaste la equivocada (la de tu EOA en vez del Funder).

---

## 4. Paso B — Obtener tu Private Key

> **ADVERTENCIA:** La private key da control total sobre las órdenes que
> firma tu cuenta. Tratala como el PIN de tu banco: nunca la compartas,
> no la pegues en chats, no la subas a git. El `.env` está en
> `.gitignore` por default pero verificalo antes de hacer un commit.

### Si elegiste Opción A (email/Magic):

1. En polymarket.com → Settings → "Export Private Key" o "Show Secret Key".
2. Polymarket te pide confirmación por email. Hacé click en el link.
3. Aparece la clave que empieza con `0x...`. Copiala — esa es tu
   `POLY_PRIVATE_KEY`.
4. Guardala en un gestor de contraseñas (1Password, Bitwarden) **antes** de
   pegarla en `.env`. Si la perdés, perdés acceso a tu cuenta.

### Si elegiste Opción B (MetaMask/Rabby):

1. Abrí MetaMask → click en el menú de tu cuenta (los 3 puntos o el avatar).
2. "Account Details" → "Show private key" → ingresá password de MetaMask.
3. Copiá la clave `0x...`.

**Hardware wallet:** no se puede. Creá una hot wallet nueva solo para el
bot, con un monto que estés dispuesto a perder si algo sale mal.

---

## 5. Paso C — Depositar USDC en la red Polygon

Polymarket opera con **USDC nativa en la red Polygon**. Tres palabras
clave:

- **USDC** (no USDT, no DAI)
- **Nativa** (no USDC.e, que es la bridged vieja)
- **Polygon** (no Ethereum mainnet, no Arbitrum)

Si te equivocás en cualquiera de las tres, perdés los fondos o tardan
horas en llegar.

### Rutas típicas para llegar a USDC en Polygon

**Desde un exchange (Binance, Coinbase, Kraken):**

1. En el exchange, iniciá un retiro de USDC.
2. **Red:** elegí "Polygon" (a veces aparece como "MATIC" — es lo mismo).
3. **Dirección de destino:** tu `POLY_FUNDER` (NO tu EOA).
4. Mandá primero **$10 de prueba** para confirmar que llega. Una vez
   confirmado en <https://polygonscan.com>, mandá el resto.

**Desde Ethereum mainnet:**

1. Usá el bridge oficial: <https://portal.polygon.technology/bridge>.
2. Puentea USDC de Ethereum → Polygon. Tarda 10-20 minutos.
3. Una vez en tu wallet de Polygon, transferí a `POLY_FUNDER`.

**Sin cripto previa:**

- En polymarket.com: Deposit → "On-Ramp". Permite comprar con tarjeta vía
  MoonPay o similares. Más caro (fees 2-5%) pero funciona.

### Cuánto depositar

| Escenario | Monto recomendado |
|---|---|
| Paper trading | **$0** — no se necesita ningún fondo |
| Primera semana en live | $100 - $200 |
| Live estable con todos los caps del risk manager | $1000+ |

El bot usa `TOTAL_CAPITAL_USDC` del `.env` para calcular los caps.
Ejemplos con `TOTAL_CAPITAL_USDC=200`:

- Max por mercado: 5% × 200 = **$10**
- Max por estrategia: 25% × 200 = **$50**
- Daily loss cap: 5% × 200 = **$10** (después se activa kill switch)
- Monthly loss cap: 15% × 200 = **$30**

Para $200 tiene sentido bajar `DEFAULT_TRADE_SIZE_USDC=10` en el `.env`.

### Verificar que el depósito llegó

1. Andá a <https://polygonscan.com/address/TU_FUNDER>.
2. Tab "Token Holdings" → buscá "USD Coin (USDC)".
3. Debería aparecer el monto que depositaste.

Alternativa: en polymarket.com el balance aparece arriba a la derecha.

---

## 6. Paso D — Aprobar contratos (solo wallet externa)

**Si usás Opción A (email/Magic), saltate este paso.** Polymarket hace los
approvals automáticamente.

Si usás **Opción B (MetaMask/Rabby)**, la primera vez tenés que dar
permiso a los contratos de Polymarket para que puedan mover tu USDC y tus
conditional tokens. Esto se hace **una sola vez** por wallet.

### Forma simple

1. Entrá a polymarket.com con tu wallet conectada.
2. Hacé una **orden manual de $1-5** en cualquier mercado líquido.
3. Polymarket te va a pedir firmar 2-3 transacciones de approval:
   - Approve USDC for CTF Exchange
   - Approve Conditional Tokens for CTF Exchange
   - (A veces) Approve para neg-risk adapter
4. Firmá todas. Pagás ~$0.02 de gas en MATIC por cada una. Necesitás
   tener **al menos $0.50 de MATIC** en la wallet para el gas.

Una vez que Polymarket aceptó la orden de prueba, el bot puede operar
sin más approvals.

### Contratos involucrados (referencia)

- **CTF Exchange legacy (binarios):** `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`
- **CTF Exchange neg-risk:** `0xC5d563A36AE78145C45a50134d48A1215220f80a`
- **Neg-risk adapter:** `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`

Tokens a aprobar:

- **USDC:** `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`
- **Conditional Tokens:** `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`

Si querés automatizar los approvals con un script de Python, hay uno de
referencia en <https://gist.github.com/poly-rodr/44313920481de58d5a3f6d1f8226bd5e>.

---

## 7. Paso E — Configurar `.env`

Copiá la plantilla y editala:

```bash
cp .env.example .env
```

### Ejemplo mínimo para Opción A ($200 de capital, modo paper)

```bash
# === MODO ===
MODE=paper
LOG_LEVEL=INFO
STRATEGIES_ENABLED=arbitrage,tail_end

# === POLYMARKET (Opción A, email/Magic) ===
POLY_PRIVATE_KEY=0x<la-clave-que-exportaste-de-polymarket>
POLY_FUNDER=0x<tu-proxy-wallet-del-perfil>
POLY_SIGNATURE_TYPE=1

# === RPC (Alchemy free tier alcanza) ===
ALCHEMY_HTTP_URL=https://polygon-mainnet.g.alchemy.com/v2/TU_KEY
ALCHEMY_WSS_URL=wss://polygon-mainnet.g.alchemy.com/v2/TU_KEY

# === CAPITAL ===
TOTAL_CAPITAL_USDC=200
DEFAULT_TRADE_SIZE_USDC=10

# === (opcional) TELEGRAM ===
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### Ejemplo para Opción B (MetaMask)

Igual que arriba pero con `POLY_SIGNATURE_TYPE=0`:

```bash
POLY_PRIVATE_KEY=0x<clave-de-tu-metamask>
POLY_FUNDER=0x<proxy-que-polymarket-te-genero>
POLY_SIGNATURE_TYPE=0
```

### Dónde conseguir el RPC de Alchemy

1. Creá cuenta en <https://dashboard.alchemy.com>.
2. "Create new app" → Chain: **Polygon**, Network: **Polygon Mainnet**.
3. Copiá las URLs HTTPS y WSS al `.env`.

El free tier (300M compute units por mes) alcanza de sobra para este bot.

### Verificar antes de correr

```bash
# Que .env NO esté en git
git status             # .env no debería aparecer
grep "^\.env$" .gitignore   # debería matchear

# Que las variables críticas estén definidas
grep -E "^(POLY_PRIVATE_KEY|POLY_FUNDER|MODE|ALCHEMY_HTTP_URL)=" .env
```

---

## 8. Paso F — Validación paper antes de live

Corré el bot:

```bash
source .venv/bin/activate    # si usás venv
python main.py
```

### Qué deberías ver si está todo bien

**En el dashboard Rich (terminal):**

- Header con `[PAPER]` en amarillo, tu capital, PnL en cero.
- Panel "Connectivity": `RPC  alchemy OK`.
- Panel "Strategies": las estrategias listadas en `active`.
- Panel "Opportunity Queue": empieza vacío, va llenándose a medida que
  el scanner descubre oportunidades.
- Panel "Recent Fills": va acumulando fills con status `simulated`.

**En los logs:**

```
main               Polymarket Multi-Strategy Bot starting | mode=paper | ...
main               Connected to Polygon chain_id=137 rpc=https://polygon-mainnet.g.alchemy.com/...
main               Loaded 2 strategies: ['arbitrage', 'tail_end']
scanner            Scanner started (interval=5s, enrich_top=40)
executor           Executor started mode=paper
main               heartbeat block=... queue=... fills=...
```

**Qué NO deberías ver:**

- `CLOB client authenticated` — en paper mode no se inicializa el cliente
  real, así que este log no aparece. Es correcto.
- `Cannot reach Polygon RPC` — si lo ves, revisá `ALCHEMY_HTTP_URL`.
- `Too many API errors; global pause` — tu RPC está sobrecargado o tiene
  límites muy bajos.

### Cuánto tiempo dejar en paper

Mínimo **24 horas**. Ideal: 7 días, para que veas cómo performa en
distintos contextos de mercado (weekend quieto, apertura US, etc.).

Revisá `bot_state.json` periódicamente:

```bash
cat bot_state.json | python -m json.tool | grep -A2 stats
```

Te va a mostrar win rate y PnL teórico por estrategia.

---

## 9. Cómo pasar a live

Cuando estés convencido de que todo funciona en paper:

1. **Confirmá que hay USDC en tu Funder** (ver Paso C).
2. **Confirmá approvals** (solo Opción B — hacé una orden manual de $1
   en polymarket.com si no lo hiciste aún).
3. Editá `.env`:
   ```bash
   MODE=live
   ```
4. Reiniciá el bot:
   ```bash
   python main.py
   ```
5. En los logs debería aparecer:
   ```
   polymarket         CLOB client authenticated.
   main               Polymarket Multi-Strategy Bot starting | mode=live | ...
   ```
   **Ese "CLOB client authenticated" es la confirmación definitiva** de que
   tu private key + funder + signature_type están bien configurados.

### Si "CLOB client authenticated" no aparece

El bot aborta con un error. Las causas más comunes:

| Error | Causa | Fix |
|---|---|---|
| `invalid private key` | La clave tiene espacios, salto de línea o le falta `0x` | Re-copiala desde tu gestor de contraseñas |
| `signature verification failed` | `POLY_SIGNATURE_TYPE` incorrecto | Probá `0` si tenés `1`, o `1` si tenés `0` |
| `funder address not found` | Pegaste tu EOA en `POLY_FUNDER` | Volvé al Paso A y copiá la del perfil público |
| `rate limit exceeded` al arrancar | Tu key de Alchemy se agotó | Generá una nueva o esperá al reset mensual |

---

## 10. Checklist de seguridad antes de live

Antes de darle al bot acceso a fondos reales, confirmá **cada uno** de estos puntos:

- [ ] `.env` está en `.gitignore`. Verificalo con `git status` — `.env` no
      debe aparecer en la lista de archivos.
- [ ] Probaste 24-48 horas en `MODE=paper` sin errores persistentes.
- [ ] El monto en el Funder es el que estás **dispuesto a perder
      completamente**. El bot puede tener bugs, la API puede fallar, la
      blockchain puede tener un incidente.
- [ ] La wallet que usás es **dedicada al bot**, NO tu wallet principal.
      Si tu wallet principal tiene $50k, la del bot debería tener $200-500.
- [ ] Tenés backup de la private key en un gestor de contraseñas.
- [ ] Configuraste Telegram (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`) y
      le mandaste `/status` para confirmar que recibís mensajes.
- [ ] Sabés cómo usar `/emergencystop` desde Telegram para parar el bot
      en cualquier momento.
- [ ] El VPS/máquina donde corre el bot tiene disco cifrado.
- [ ] `TOTAL_CAPITAL_USDC` refleja el balance real del Funder, no un
      valor inflado. Si mentís, los caps del risk manager son ficticios.

---

## 11. Errores comunes y cómo evitarlos

| Síntoma | Causa más probable | Cómo arreglar |
|---|---|---|
| El USDC nunca llegó al Funder | Lo mandaste por red Ethereum mainnet o a USDC.e | Si todavía está confirmándose podés cancelar; si ya se minteó, hay que bridgearlo manualmente |
| Lo mandaste a tu EOA en vez del Funder | Confundiste direcciones | Desde tu EOA, transferí el USDC al Funder (paga gas en MATIC) |
| `CLOB client authentication failed` | `POLY_SIGNATURE_TYPE` mal | Probá los 3 valores (0, 1, 2) hasta que funcione |
| `insufficient balance` en logs aunque hay USDC | El balance está en USDC.e (bridged) en vez de USDC nativa | Swapeá a USDC nativa en Uniswap Polygon |
| El bot arranca pero nunca ejecuta nada | Approvals faltantes (Opción B) | Hacé una orden manual de $1 en polymarket.com |
| Slippage skips en todos los trades | RPC lento (público o saturado) | Upgradeá a Alchemy/QuickNode private tier |
| Heartbeat muestra `block=err(...)` | `ALCHEMY_HTTP_URL` mal o sin quota | Re-generá key en dashboard.alchemy.com |
| El bot se queda colgado al arrancar | MATIC insuficiente para gas | Depositá 1-2 MATIC en la EOA |

---

## 12. Preguntas frecuentes

**¿Puedo usar la misma wallet para múltiples bots?**
Sí técnicamente, pero cada bot tendría visibilidad sobre los fondos de
todos. Mejor una wallet por bot con un presupuesto definido.

**¿El bot puede retirar fondos a mi wallet personal?**
No directamente. El bot solo firma órdenes CLOB (buy/sell). Para retirar
USDC del Funder a tu EOA tenés que usar la UI de Polymarket (Withdraw)
o un script aparte.

**¿Qué pasa si mi cuenta Magic se bloquea?**
Si perdés acceso al email, perdés la cuenta. Por eso recomiendo migrar a
Opción B (wallet externa) una vez que validaste que el bot funciona —
ahí la custodia es 100% tuya.

**¿Necesito MATIC en la wallet?**
Sí, un poco (~$1-2 en MATIC) para pagar gas cuando el bot firma
transacciones. En paper mode no hace falta.

**¿Cuánto tarda un trade en ejecutarse?**
De punta a punta: ~500ms - 2s (firma + red Polygon + confirmación on-chain).
El bot usa órdenes FOK para arbitrage, que o se llenan inmediatamente o se
cancelan.

**¿El bot puede hacer retiros automáticos de profits?**
No por default. Para hacer eso tendrías que escribir un módulo separado
que mueva USDC del Funder a una wallet de "profits" cuando supera cierto
umbral. Fuera del scope actual.

**¿Qué hago si el bot pierde dinero y se activa el daily loss cap?**
El kill switch se activa automáticamente. Revisá los logs y el
`bot_state.json` para entender qué estrategia falló. Podés:
- Deshabilitar esa estrategia en `STRATEGIES_ENABLED`.
- Mandar `/resume` por Telegram para continuar.
- O dejar el bot detenido hasta el próximo día (el cap se resetea a la
  medianoche UTC).

**¿Dónde veo mis posiciones?**
Tres lugares:
- En tiempo real: dashboard Rich en la terminal.
- Persistencia local: `bot_state.json` (`positions_by_strategy`).
- En polymarket.com → Portfolio (siempre es la fuente de verdad).

---

## Resumen en 60 segundos

```
1. Entrá a polymarket.com, logueate con email.
2. Copiá la dirección de tu perfil (0x...) → POLY_FUNDER.
3. Settings → Export private key → POLY_PRIVATE_KEY.
4. Desde Binance, retirá USDC por red Polygon a tu POLY_FUNDER.
   Mandá $10 de prueba primero.
5. Copiá .env.example a .env, llenalo con los datos de arriba y
   MODE=paper.
6. python main.py → dejalo 24h.
7. Si todo anda, flipeá MODE=live y reiniciá.
```

Con eso operás. Si algo falla, revisá primero "Errores comunes" arriba;
el 90% de los problemas están ahí.

---

## Referencias

- [Polymarket Docs — Quickstart](https://docs.polymarket.com/trading/quickstart)
- [Polymarket Docs — Getting Started](https://docs.polymarket.com/market-makers/getting-started)
- [py-clob-client README](https://github.com/Polymarket/py-clob-client)
- [docs/SETUP.md](SETUP.md) — setup general del bot (RPC, Supabase, PM2).
- [docs/STRATEGIES.md](STRATEGIES.md) — cuándo usar cada estrategia.
