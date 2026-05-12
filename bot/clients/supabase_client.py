"""Supabase mirror for fills and paper PnL.

Optional — if SUPABASE_URL / SUPABASE_KEY are unset the StateStore simply
skips this layer. Table schema (create in your Supabase project):

```sql
create table fills (
    id            uuid primary key default gen_random_uuid(),
    ts            timestamptz default now(),
    strategy      text not null,
    market_id     text not null,
    status        text not null,
    pnl_usdc      numeric,
    reason        text,
    details       jsonb
);
create index on fills (strategy, ts desc);
create index on fills (market_id);
```

Usage
-----
>>> from bot.clients.supabase_client import SupabaseMirror
>>> mirror = SupabaseMirror(url, key)
>>> mirror.insert_fill(fill)
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from bot.logger import get_logger
from bot.models import Fill

log = get_logger("supabase")

try:
    from supabase import create_client, Client  # type: ignore
except Exception:  # noqa: BLE001
    create_client = None  # type: ignore[assignment]
    Client = Any  # type: ignore[assignment,misc]


class SupabaseMirror:
    def __init__(self, url: str, key: str) -> None:
        if create_client is None:
            raise RuntimeError("supabase package not installed")
        self._client: Client = create_client(url, key)

    def insert_fill(self, fill: Fill) -> None:
        payload = {
            "strategy": fill.strategy,
            "market_id": fill.market_id,
            "status": fill.status,
            "pnl_usdc": fill.pnl_usdc,
            "reason": fill.reason,
            "details": {
                "opportunity_id": fill.opportunity_id,
                "tx_hashes": fill.tx_hashes,
                **(fill.details or {}),
            },
        }
        try:
            self._client.table("fills").insert(payload).execute()
        except Exception as e:  # noqa: BLE001
            log.debug(f"Supabase insert_fill failed: {e}")

    def upsert_stats(self, strategy: str, stats: dict[str, Any]) -> None:
        try:
            self._client.table("strategy_stats").upsert(
                {"strategy": strategy, **stats}
            ).execute()
        except Exception as e:  # noqa: BLE001
            log.debug(f"Supabase upsert_stats failed: {e}")
