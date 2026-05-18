#!/usr/bin/env python
"""Quick analytics of the bot's paper-trading session.

Run from the bot dir:
    python scripts/paper_report.py

Reads bot_state.json + bot_paper.log and prints:
  * paper fills count + cumulative PnL
  * per-strategy breakdown
  * skip-reason histogram (how many opportunities each filter rejected)
  * extrapolated monthly return at the simulated capital
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ANSI = re.compile(r"\x1b\[[0-9;]*m")
HERE = Path(__file__).resolve().parent.parent


def load_state():
    with open(HERE / "bot_state.json") as f:
        return json.load(f)


def read_log():
    for name in ("bot_paper.log", "bot_live.log"):
        p = HERE / name
        if p.exists():
            return ANSI.sub("", p.read_text(errors="ignore")), name
    return "", None


def main() -> int:
    state = load_state()
    log, log_name = read_log()

    fills = state.get("paper_fills", [])
    stats = state.get("stats", {})
    capital = 0.0
    for line in log.splitlines():
        m = re.search(r"Capital \$(\d+\.\d+)", line)
        if m:
            capital = float(m.group(1))

    first_ts = None
    last_ts = None
    for f in fills:
        ts = f.get("executed_at")
        if ts:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)

    print("═" * 60)
    print(f"  PAPER TRADING REPORT  (log={log_name})")
    print("═" * 60)
    print(f"Capital simulado:     ${capital:.2f}")
    print(f"Fills totales:        {len(fills)}")
    if first_ts and last_ts:
        span_h = (last_ts - first_ts) / 3600
        print(
            f"Ventana:              "
            f"{datetime.fromtimestamp(first_ts, tz=timezone.utc):%Y-%m-%d %H:%M}"
            f"  →  "
            f"{datetime.fromtimestamp(last_ts, tz=timezone.utc):%Y-%m-%d %H:%M}"
            f"  ({span_h:.1f}h)"
        )

    # PnL
    total_pnl = sum(f.get("pnl_usdc", 0) or 0 for f in fills)
    wins = sum(1 for f in fills if (f.get("pnl_usdc") or 0) > 0)
    losses = sum(1 for f in fills if (f.get("pnl_usdc") or 0) < 0)
    flat = len(fills) - wins - losses
    winrate = wins / max(wins + losses, 1)

    print()
    print(f"  PnL total:          ${total_pnl:+.4f}")
    if capital > 0:
        print(f"  Return on capital:  {(total_pnl/capital)*100:+.2f}%")
    print(f"  Wins / Losses / —:  {wins} / {losses} / {flat}")
    print(f"  Win rate:           {winrate*100:.1f}%")

    # Monthly extrapolation
    if first_ts and last_ts and capital > 0:
        span_h = max((last_ts - first_ts) / 3600, 0.001)
        pnl_per_hour = total_pnl / span_h
        monthly = pnl_per_hour * 24 * 30
        print(
            f"  Extrapolación mensual (si el edge es estable): "
            f"${monthly:+.2f}  ({(monthly/capital)*100:+.1f}%)"
        )

    # Per-strategy
    print()
    print("  Por estrategia:")
    per_strat: dict[str, dict[str, float]] = {}
    for f in fills:
        s = f.get("strategy", "?")
        per_strat.setdefault(s, {"n": 0, "pnl": 0.0, "win": 0, "loss": 0})
        per_strat[s]["n"] += 1
        pnl = f.get("pnl_usdc") or 0
        per_strat[s]["pnl"] += pnl
        if pnl > 0:
            per_strat[s]["win"] += 1
        elif pnl < 0:
            per_strat[s]["loss"] += 1
    for s, d in sorted(per_strat.items()):
        wr = d["win"] / max(d["win"] + d["loss"], 1)
        print(
            f"    {s:<12}  trades={int(d['n']):>3}  "
            f"pnl=${d['pnl']:+.3f}  wr={wr*100:.0f}%"
        )
    if not per_strat:
        print("    (sin fills aún)")

    # Skip reasons histogram
    print()
    print("  Filtros que rechazaron oportunidades:")
    skip_re = re.compile(r"skip (?:\(([^)]+)\)|wallet [^:]+: (\S+))")
    blocked_re = re.compile(r"blocked:\s*(\S+)")
    counts: Counter[str] = Counter()
    for line in log.splitlines():
        for m in skip_re.finditer(line):
            reason = m.group(1) or m.group(2)
            if reason:
                counts[reason.split()[0]] += 1
        for m in blocked_re.finditer(line):
            counts[f"blocked:{m.group(1)}"] += 1
    if not counts:
        print("    (ningún skip registrado todavía)")
    for reason, n in counts.most_common(12):
        print(f"    {reason:<30}  {n:>4}")

    # Heartbeats
    hb = sum(1 for _ in re.finditer(r"heartbeat", log))
    print()
    print(f"  Heartbeats detectados: {hb}")
    print("═" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
