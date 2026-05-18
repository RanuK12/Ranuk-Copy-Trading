"""One-shot script: close stale positions and discover profitable wallets.

Usage: .venv/bin/python scripts/cleanup_and_discover.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.clients.polymarket import get_poly
from bot.config import CFG
from bot.state import get_state
from bot.logger import get_logger

log = get_logger("cleanup")


async def main():
    state = get_state()
    poly = get_poly()

    print("\n" + "=" * 60)
    print("📊 EVALUACIÓN DE POSICIONES ABIERTAS")
    print("=" * 60)

    positions = state.state.positions_by_strategy.get("smart_copy", [])
    open_positions = [p for p in positions if p.get("open")]
    print(f"\nPosiciones abiertas: {len(open_positions)}")

    paper_closed = 0
    live_to_sell = []

    for pos in open_positions:
        market_id = pos.get("market_id", "")[:20]
        size = pos.get("size_usdc", 0)
        legs = pos.get("legs", [])

        # Paper positions — close them all (stale from previous session)
        if pos.get("paper"):
            entry_price = legs[0].get("price", 0) if legs else 0
            token_id = legs[0].get("token_id", "") if legs else ""
            current = None
            if token_id:
                current = poly.get_price(token_id, "SELL")

            pnl_pct = ((current - entry_price) / entry_price * 100) if current and entry_price else 0
            status = "🟢" if pnl_pct >= 0 else "🔴"
            print(f"\n  {status} [PAPER] {market_id}...")
            print(f"     Entry: {entry_price:.4f} | Now: {current or '?'} | PnL: {pnl_pct:+.1f}%")

            # Close all paper positions (they're stale)
            pnl = (current - entry_price) * (size / entry_price) if current and entry_price else -size * 0.5
            state.close_position("smart_copy", pos["market_id"], pnl)
            paper_closed += 1
            print(f"     → CERRADA (PnL: {pnl:+.4f} USDC)")

        # Live positions — check if they have token info
        elif pos.get("ok_count"):
            print(f"\n  ⚡ [LIVE] {market_id}...")
            print(f"     Size: ${size:.2f} | Status: {legs[0].get('response', {}).get('status', '?') if legs else '?'}")

            # These live positions don't have token_id stored in state
            # (the executor stored the CLOB response, not the leg details)
            # We'll mark them as closed with estimated loss since we can't
            # fetch their current price without the token_id
            resp = legs[0].get("response", {}) if legs else {}
            if resp.get("status") == "delayed":
                # Order was delayed = likely never filled
                print(f"     → Status 'delayed' = probablemente no se llenó. Cerrando con PnL=0")
                state.close_position("smart_copy", pos["market_id"], 0.0)
                paper_closed += 1
            elif resp.get("status") == "matched":
                # This one actually filled — we need to sell it
                print(f"     → MATCHED (filled). Necesita venta manual o via monitor.")
                live_to_sell.append(pos)
            else:
                state.close_position("smart_copy", pos["market_id"], 0.0)
                paper_closed += 1

    print(f"\n{'=' * 60}")
    print(f"✅ Cerradas: {paper_closed} posiciones")
    print(f"⚠️  Live pendientes de venta: {len(live_to_sell)}")

    # ---- WALLET DISCOVERY ----
    print(f"\n{'=' * 60}")
    print("🔍 BUSCANDO WALLETS RENTABLES EN LEADERBOARD")
    print("=" * 60)

    from bot.wallet_discovery import WalletDiscovery
    discovery = WalletDiscovery()
    await discovery._discover_and_update()

    print(f"\n✅ Smart wallets actualizadas: {len(CFG.smart_wallets)}")
    for i, w in enumerate(CFG.smart_wallets[:10], 1):
        print(f"   {i}. {w}")

    # Save updated wallets to .env
    if CFG.smart_wallets:
        wallets_str = ",".join(CFG.smart_wallets[:10])
        env_path = Path(__file__).resolve().parent.parent / ".env"
        env_content = env_path.read_text()

        if "SMART_WALLETS=" in env_content:
            import re
            env_content = re.sub(
                r"SMART_WALLETS=.*",
                f"SMART_WALLETS={wallets_str}",
                env_content,
            )
        else:
            env_content += f"\nSMART_WALLETS={wallets_str}\n"

        env_path.write_text(env_content)
        print(f"\n✅ .env actualizado con {len(CFG.smart_wallets[:10])} wallets")

    state.save()
    print(f"\n✅ State guardado.")


if __name__ == "__main__":
    asyncio.run(main())
