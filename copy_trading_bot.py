"""
Polymarket Copy-Trading Bot
===========================

Monitors up to 10 "smart money" Polygon wallets and replicates their
Polymarket trades with a fixed USDC notional, honoring a max-slippage rule
and deduplication on already-copied markets.

Two execution modes:
  * PAPER_TRADING=true  -> no on-chain tx; prints simulations + theoretical PnL
  * PAPER_TRADING=false -> posts real market orders through py-clob-client

All configuration is loaded from environment variables (see .env.example).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from rich.logging import RichHandler
from web3 import Web3

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False, markup=True)],
)
log = logging.getLogger("copybot")


def _env(name: str, default: Optional[str] = None, *, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and not val:
        log.error(f"[red]Missing required env var:[/] {name}")
        sys.exit(1)
    return val or ""


@dataclass(frozen=True)
class Config:
    paper: bool = _env("PAPER_TRADING", "true").lower() == "true"
    http_rpc: str = _env("POLYGON_HTTP_RPC", required=True)
    wss_rpc: str = _env("POLYGON_WSS_RPC", "")
    clob_host: str = _env("CLOB_HOST", "https://clob.polymarket.com")
    data_api: str = _env("DATA_API_HOST", "https://data-api.polymarket.com")
    gamma_api: str = _env("GAMMA_API_HOST", "https://gamma-api.polymarket.com")
    private_key: str = _env("PRIVATE_KEY", "")
    funder: str = _env("FUNDER_ADDRESS", "")
    signature_type: int = int(_env("SIGNATURE_TYPE", "1"))
    smart_wallets: tuple[str, ...] = tuple(
        w.strip().lower()
        for w in _env("SMART_WALLETS", required=True).split(",")
        if w.strip()
    )[:10]  # hard cap at 10
    trade_amount_usdc: float = float(_env("TRADE_AMOUNT_USDC", "20"))
    max_slippage: float = float(_env("MAX_SLIPPAGE", "0.02"))
    only_buys: bool = _env("ONLY_BUYS", "true").lower() == "true"
    poll_interval: float = float(_env("POLL_INTERVAL_SECONDS", "3"))
    heartbeat_interval: float = float(_env("BLOCK_HEARTBEAT_SECONDS", "15"))
    state_file: Path = Path(_env("STATE_FILE", "./bot_state.json"))


CFG = Config()


# ---------------------------------------------------------------------------
# Persistent state (duplicate-prevention + paper-trading ledger)
# ---------------------------------------------------------------------------
@dataclass
class State:
    copied_markets: set[str] = field(default_factory=set)  # conditionIds already copied
    last_seen_tx: dict[str, str] = field(default_factory=dict)  # wallet -> last txHash
    paper_positions: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text())
            return cls(
                copied_markets=set(raw.get("copied_markets", [])),
                last_seen_tx=dict(raw.get("last_seen_tx", {})),
                paper_positions=list(raw.get("paper_positions", [])),
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"Could not read state file ({e}); starting fresh.")
            return cls()

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "copied_markets": sorted(self.copied_markets),
                    "last_seen_tx": self.last_seen_tx,
                    "paper_positions": self.paper_positions,
                },
                indent=2,
            )
        )


STATE = State.load(CFG.state_file)


# ---------------------------------------------------------------------------
# Web3 + CLOB client
# ---------------------------------------------------------------------------
w3 = Web3(Web3.HTTPProvider(CFG.http_rpc, request_kwargs={"timeout": 10}))
if not w3.is_connected():
    log.error(f"[red]Cannot reach Polygon RPC:[/] {CFG.http_rpc}")
    sys.exit(1)
log.info(f"[green]Connected to Polygon[/] chain_id={w3.eth.chain_id} rpc={CFG.http_rpc[:60]}...")

clob_client = None  # Lazily initialized so paper mode can run without a key
if not CFG.paper:
    from py_clob_client.client import ClobClient  # type: ignore
    from py_clob_client.clob_types import MarketOrderArgs, OrderType  # type: ignore
    from py_clob_client.order_builder.constants import BUY, SELL  # type: ignore

    if not CFG.private_key or not CFG.funder:
        log.error("[red]PRIVATE_KEY and FUNDER_ADDRESS are required for live trading.[/]")
        sys.exit(1)

    clob_client = ClobClient(
        CFG.clob_host,
        key=CFG.private_key,
        chain_id=137,
        signature_type=CFG.signature_type,
        funder=CFG.funder,
    )
    clob_client.set_api_creds(clob_client.create_or_derive_api_creds())
    log.info("[green]CLOB client authenticated.[/]")
else:
    log.info("[yellow]PAPER_TRADING=true -> no real orders will be sent.[/]")


# ---------------------------------------------------------------------------
# Polymarket API helpers
# ---------------------------------------------------------------------------
_session = requests.Session()


def get_recent_trades(wallet: str, limit: int = 20) -> list[dict[str, Any]]:
    """Latest trades for a wallet via the public Data-API (most recent first)."""
    url = f"{CFG.data_api}/trades"
    try:
        r = _session.get(url, params={"user": wallet, "limit": limit}, timeout=8)
        r.raise_for_status()
        return r.json() or []
    except Exception as e:  # noqa: BLE001
        log.warning(f"Data-API trades fetch failed for {wallet[:10]}...: {e}")
        return []


def get_current_price(token_id: str, side: str = "BUY") -> Optional[float]:
    """Current best-bid/ask price (0..1) from the CLOB, used for slippage check."""
    try:
        r = _session.get(
            f"{CFG.clob_host}/price",
            params={"token_id": token_id, "side": side.upper()},
            timeout=5,
        )
        r.raise_for_status()
        return float(r.json().get("price"))
    except Exception as e:  # noqa: BLE001
        log.warning(f"Price fetch failed for token {token_id[:12]}...: {e}")
        return None


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------
def place_market_order(token_id: str, usdc_amount: float, side: str) -> dict[str, Any]:
    """Send a FOK market order via the CLOB. Returns the raw response dict."""
    assert clob_client is not None, "CLOB client not initialized"
    side_const = BUY if side.upper() == "BUY" else SELL
    args = MarketOrderArgs(
        token_id=token_id,
        amount=float(usdc_amount),
        side=side_const,
        order_type=OrderType.FOK,
    )
    signed = clob_client.create_market_order(args)
    return clob_client.post_order(signed, OrderType.FOK)


def simulate_trade(trade: dict[str, Any], current_price: float) -> None:
    """Record a paper-trade fill and log theoretical PnL vs. detected price."""
    detected_price = float(trade["price"])
    shares = CFG.trade_amount_usdc / current_price if current_price else 0.0
    # Theoretical PnL = how much we'd be up/down if we'd exited at detected price
    # (sanity check against the smart-money entry). In practice you want the LIVE price later.
    theoretical_pnl = (current_price - detected_price) * shares
    entry = {
        "ts": int(time.time()),
        "market": trade.get("slug") or trade.get("conditionId"),
        "condition_id": trade.get("conditionId"),
        "token_id": trade.get("asset"),
        "side": trade.get("side"),
        "detected_price": detected_price,
        "entry_price": current_price,
        "shares": round(shares, 4),
        "usdc": CFG.trade_amount_usdc,
        "theoretical_pnl_vs_detected": round(theoretical_pnl, 4),
    }
    STATE.paper_positions.append(entry)
    log.info(
        f"[cyan]Simulación de Trade:[/] {entry['market']} - {entry['side']} - "
        f"entry={current_price:.4f} shares={entry['shares']:.2f} "
        f"theoretical_pnl={theoretical_pnl:+.4f} USDC"
    )


# ---------------------------------------------------------------------------
# Core copy-trading logic
# ---------------------------------------------------------------------------
async def process_trade(wallet: str, trade: dict[str, Any], block_number: int) -> None:
    condition_id = trade.get("conditionId")
    token_id = trade.get("asset")
    side = (trade.get("side") or "").upper()
    detected_price = float(trade.get("price") or 0)
    slug = trade.get("slug") or condition_id

    tag = f"[blue]blk={block_number}[/] wallet={wallet[:10]}.. market={slug} side={side} price={detected_price:.4f}"

    if not condition_id or not token_id or not side:
        log.debug(f"{tag} -> [yellow]skipped (incomplete trade payload)[/]")
        return

    if CFG.only_buys and side != "BUY":
        log.debug(f"{tag} -> [yellow]skipped (not a BUY)[/]")
        return

    # --- Duplicate guard --------------------------------------------------
    if condition_id in STATE.copied_markets:
        log.info(f"{tag} -> [yellow]duplicate: market already copied, skipping.[/]")
        return

    # --- Slippage guard ---------------------------------------------------
    current_price = get_current_price(token_id, side)
    if current_price is None:
        log.warning(f"{tag} -> [red]no current price; skipping.[/]")
        return
    max_price = detected_price * (1 + CFG.max_slippage)
    if current_price > max_price:
        log.warning(
            f"{tag} -> [red]SLIPPAGE EXCEEDED[/] "
            f"(current={current_price:.4f} > max={max_price:.4f}); skipping."
        )
        return

    # --- Execute ----------------------------------------------------------
    try:
        if CFG.paper:
            simulate_trade(trade, current_price)
        else:
            resp = await asyncio.to_thread(
                place_market_order, token_id, CFG.trade_amount_usdc, side
            )
            log.info(f"{tag} -> [green]ORDER POSTED[/] resp={resp}")
        STATE.copied_markets.add(condition_id)
        STATE.save(CFG.state_file)
    except Exception as e:  # noqa: BLE001
        log.exception(f"{tag} -> [red]execution failed:[/] {e}")


async def watch_wallet(wallet: str) -> None:
    """Poll one smart-money wallet and emit any new trades for copying."""
    log.info(f"[green]Watching[/] {wallet}")
    while True:
        trades = await asyncio.to_thread(get_recent_trades, wallet, 10)
        block = w3.eth.block_number
        last_seen = STATE.last_seen_tx.get(wallet)

        # Trades are returned most-recent first; walk oldest-first among the *new* ones.
        new_trades: list[dict[str, Any]] = []
        for t in trades:
            if t.get("transactionHash") == last_seen:
                break
            new_trades.append(t)
        for t in reversed(new_trades):  # chronological order
            await process_trade(wallet, t, block)

        if trades:
            STATE.last_seen_tx[wallet] = trades[0].get("transactionHash", last_seen or "")
            STATE.save(CFG.state_file)

        await asyncio.sleep(CFG.poll_interval)


async def block_heartbeat() -> None:
    """Periodically log the current Polygon block so we can see the bot is alive."""
    while True:
        try:
            blk = w3.eth.block_number
            log.info(
                f"[grey]heartbeat[/] block={blk} watching={len(CFG.smart_wallets)} "
                f"copied={len(STATE.copied_markets)} paper_positions={len(STATE.paper_positions)}"
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"heartbeat RPC error: {e}")
        await asyncio.sleep(CFG.heartbeat_interval)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
async def main() -> None:
    if not CFG.smart_wallets:
        log.error("[red]No SMART_WALLETS configured; aborting.[/]")
        return
    log.info(
        f"[bold]Polymarket Copy-Bot starting[/] | mode="
        f"{'PAPER' if CFG.paper else 'LIVE'} | wallets={len(CFG.smart_wallets)} | "
        f"notional={CFG.trade_amount_usdc} USDC | max_slippage={CFG.max_slippage*100:.1f}%"
    )
    tasks = [asyncio.create_task(block_heartbeat())]
    tasks += [asyncio.create_task(watch_wallet(w)) for w in CFG.smart_wallets]

    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: stop.set_result(None))
        except NotImplementedError:
            pass  # Windows

    await stop
    log.info("[yellow]Shutdown signal received; cancelling tasks...[/]")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    STATE.save(CFG.state_file)
    log.info("[green]State persisted. Bye.[/]")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
