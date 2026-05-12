"""Session report generator — writes an HTML summary on shutdown.

Produces ``logs/session_YYYYMMDD_HHMMSS.html`` with:

* Headline stats (mode, start/end, duration, equity delta).
* Per-strategy table (trades / win rate / PnL / expectancy).
* Equity curve (inline SVG sparkline — no external CDN).
* Skip/error histogram ("why did we skip?")
* Actionable recommendations ("tail_end has 95% WR; raise sizing.")

Zero external deps — the report is a self-contained HTML file you can
commit to git / email to yourself / drop on a static host.
"""

from __future__ import annotations

import html
import json
import os
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bot.config import CFG
from bot.logger import get_logger
from bot.risk import get_risk
from bot.state import get_state

log = get_logger("report")


def _fills_in_session(start_ts: float) -> list[dict[str, Any]]:
    fills = get_state().recent_fills(limit=500)
    return [f for f in fills if float(f.get("executed_at") or 0) >= start_ts]


def _equity_curve(fills: Iterable[dict[str, Any]]) -> list[float]:
    curve: list[float] = [0.0]
    for f in sorted(fills, key=lambda f: float(f.get("executed_at") or 0)):
        curve.append(curve[-1] + float(f.get("pnl_usdc") or 0))
    return curve


def _sparkline_svg(values: list[float], width: int = 720, height: int = 140) -> str:
    if not values or len(values) < 2:
        return f'<svg width="{width}" height="{height}"></svg>'
    lo, hi = min(values), max(values)
    span = hi - lo if hi != lo else 1.0
    step = width / (len(values) - 1)
    points = " ".join(
        f"{i * step:.2f},{height - ((v - lo) / span * (height - 4) + 2):.2f}"
        for i, v in enumerate(values)
    )
    zero_y = height - ((0 - lo) / span * (height - 4) + 2)
    color = "#3ddc84" if values[-1] >= values[0] else "#e74c3c"
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<line x1="0" y1="{zero_y:.2f}" x2="{width}" y2="{zero_y:.2f}" '
        f'stroke="#444" stroke-dasharray="4,4" stroke-width="1"/>'
        f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{points}"/>'
        "</svg>"
    )


def _recommendations(per_strat: dict[str, dict[str, Any]]) -> list[str]:
    recs: list[str] = []
    for name, s in per_strat.items():
        if s["trades"] == 0:
            continue
        wr = s["win_rate"]
        if wr >= 0.85 and s["trades"] >= 10:
            recs.append(
                f"{name} is hitting {wr * 100:.1f}% win rate over {s['trades']} "
                "trades — consider increasing its sizing multiplier via `/size`."
            )
        if wr < 0.40 and s["trades"] >= 10:
            recs.append(
                f"{name} is at {wr * 100:.1f}% win rate over {s['trades']} trades "
                "— investigate or disable with `/strat {name} off`."
            )
        if s["pnl_usdc"] < 0 and s["trades"] >= 20:
            recs.append(
                f"{name} is net negative ({s['pnl_usdc']:+.2f} USDC). "
                "Re-run the backtest before re-enabling."
            )
    return recs


def generate(
    session_start: float,
    *,
    output_dir: Path | None = None,
) -> Path:
    """Render the HTML report and return the file path."""
    end_ts = time.time()
    out_dir = output_dir or Path("logs")
    out_dir.mkdir(parents=True, exist_ok=True)

    state = get_state()
    risk = get_risk()
    rsnap = risk.snapshot()

    fills = _fills_in_session(session_start)
    curve = _equity_curve(fills)

    per_strat: dict[str, dict[str, Any]] = {}
    for name in CFG.strategies_enabled:
        s = state.get_stats(name)
        per_strat[name] = {
            "trades": s.trades,
            "wins": s.wins,
            "losses": s.losses,
            "pnl_usdc": round(s.pnl_usdc, 4),
            "win_rate": s.win_rate,
            "profit_factor": s.profit_factor,
        }

    skip_reasons = Counter(
        str(f.get("reason") or "-")
        for f in fills
        if f.get("status") == "skipped"
    )
    errors = [f for f in fills if f.get("status") == "failed"]

    # Build HTML ---------------------------------------------------------
    started_iso = datetime.fromtimestamp(session_start, tz=timezone.utc).isoformat()
    ended_iso = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()
    duration_min = (end_ts - session_start) / 60
    recs = _recommendations(per_strat)

    strat_rows = "".join(
        "<tr>"
        f"<td>{html.escape(name)}</td>"
        f"<td class='r'>{s['trades']}</td>"
        f"<td class='r'>{s['wins']}</td>"
        f"<td class='r'>{s['losses']}</td>"
        f"<td class='r'>{s['win_rate'] * 100:.1f}%</td>"
        f"<td class='r'>{s['pnl_usdc']:+.4f}</td>"
        f"<td class='r'>{s['profit_factor']:.2f}</td>"
        "</tr>"
        for name, s in per_strat.items()
    )

    skip_rows = "".join(
        f"<tr><td>{html.escape(reason)}</td><td class='r'>{count}</td></tr>"
        for reason, count in skip_reasons.most_common()
    ) or "<tr><td colspan=2 class='muted'>no skips</td></tr>"

    recs_html = "".join(f"<li>{html.escape(r)}</li>" for r in recs) or (
        "<li class='muted'>No specific recommendations yet — keep collecting data.</li>"
    )

    title = f"Polymarket Bot — Session Report ({CFG.mode})"
    body = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
 body {{ font-family: ui-monospace, Menlo, Consolas, monospace;
        background: #0d1117; color: #e6edf3; margin: 2rem; }}
 h1, h2 {{ color: #58a6ff; }}
 table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
 th, td {{ padding: 0.4rem 0.8rem; border-bottom: 1px solid #30363d;
          text-align: left; font-size: 0.95rem; }}
 th {{ background: #161b22; }}
 td.r, th.r {{ text-align: right; font-variant-numeric: tabular-nums; }}
 .muted {{ color: #8b949e; }}
 .chart {{ background: #161b22; padding: 1rem; border-radius: 8px; }}
 .kpi {{ display: inline-block; margin-right: 2rem; }}
 .kpi .v {{ font-size: 1.6rem; font-weight: bold; }}
 .kpi .l {{ color: #8b949e; font-size: 0.85rem; }}
 .pos {{ color: #3ddc84; }} .neg {{ color: #e74c3c; }}
 ul {{ line-height: 1.8; }}
</style></head><body>
<h1>{html.escape(title)}</h1>

<div>
  <span class="kpi"><div class="v">{rsnap['pnl_day']:+.2f}</div>
    <div class="l">PnL today (USDC)</div></span>
  <span class="kpi"><div class="v">{len(fills)}</div>
    <div class="l">fills</div></span>
  <span class="kpi"><div class="v">{duration_min:.1f}m</div>
    <div class="l">duration</div></span>
  <span class="kpi"><div class="v">${rsnap['equity']:.2f}</div>
    <div class="l">equity</div></span>
</div>

<h2>Equity curve</h2>
<div class="chart">{_sparkline_svg(curve)}</div>

<h2>Per-strategy</h2>
<table>
  <thead><tr><th>Strategy</th><th class="r">Trades</th><th class="r">Wins</th>
         <th class="r">Losses</th><th class="r">WR</th><th class="r">PnL</th>
         <th class="r">PF</th></tr></thead>
  <tbody>{strat_rows}</tbody>
</table>

<h2>Skip reasons</h2>
<table>
  <thead><tr><th>Reason</th><th class="r">Count</th></tr></thead>
  <tbody>{skip_rows}</tbody>
</table>

<h2>Errors ({len(errors)})</h2>
<pre class="chart">{html.escape(json.dumps(errors[-20:], indent=2, default=str)) or "none"}</pre>

<h2>Recommendations</h2>
<ul>{recs_html}</ul>

<p class="muted">Session {started_iso} → {ended_iso}. Mode: {CFG.mode}.</p>
</body></html>"""

    filename = f"session_{datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}.html"
    path = out_dir / filename
    path.write_text(body, encoding="utf-8")
    log.info(f"[green]Session report written:[/] {path}")
    return path
