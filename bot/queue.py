"""Priority queue of opportunities.

Thin async-aware wrapper around :mod:`heapq`. The scanner pushes
:class:`Opportunity` objects and the executor pops them in priority order.

Lower ``priority`` values are more urgent (arbitrage = 0). Insertion order
breaks ties.
"""

from __future__ import annotations

import asyncio
import heapq
from typing import Optional

from bot.models import Opportunity


class OpportunityQueue:
    def __init__(self, maxsize: int = 1024) -> None:
        self._heap: list[Opportunity] = []
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()
        self._maxsize = maxsize
        # Dedup: same (strategy, market_id) will not be pushed twice
        # while an instance is still pending.
        self._pending: set[tuple[str, str]] = set()

    # ---- Push -----------------------------------------------------------
    async def push(self, opp: Opportunity) -> bool:
        """Push an opportunity. Returns False if a duplicate is already queued."""
        key = (opp.strategy, opp.market_id)
        async with self._lock:
            if key in self._pending:
                return False
            if len(self._heap) >= self._maxsize:
                # Drop the lowest-priority (highest number) to make room for
                # a more urgent one — but only if the new one is more urgent.
                worst = max(self._heap)
                if opp < worst:
                    self._heap.remove(worst)
                    heapq.heapify(self._heap)
                    self._pending.discard((worst.strategy, worst.market_id))
                else:
                    return False
            heapq.heappush(self._heap, opp)
            self._pending.add(key)
            self._event.set()
            return True

    # ---- Pop ------------------------------------------------------------
    async def pop(self, timeout: Optional[float] = None) -> Optional[Opportunity]:
        """Pop the highest-priority opportunity, waiting up to ``timeout`` s."""
        try:
            if not self._heap:
                if timeout is None:
                    await self._event.wait()
                else:
                    await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

        async with self._lock:
            if not self._heap:
                self._event.clear()
                return None
            opp = heapq.heappop(self._heap)
            self._pending.discard((opp.strategy, opp.market_id))
            if not self._heap:
                self._event.clear()
            return opp

    # ---- Introspection --------------------------------------------------
    def __len__(self) -> int:
        return len(self._heap)

    def snapshot(self) -> list[Opportunity]:
        """Non-destructive copy of pending opportunities (dashboard)."""
        return sorted(self._heap)
