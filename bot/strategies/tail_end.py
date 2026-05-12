"""Strategy B — Tail-End Trading.

Buys the high-probability side of markets that resolve within
``TAIL_END_MAX_DAYS`` days and trade above ``TAIL_END_MIN_PRICE``. The
thesis: if a market has already priced in the outcome at >= 93 cents
with only days to go, continuing to hold to resolution yields a clean
2-7% per trade.

Heuristics implemented
----------------------
* days_to_resolution  in (0, MAX_DAYS]
* price of the leading side >= MIN_PRICE (default 0.93)
* stop-loss threshold set on the Opportunity for monitoring
"""

from __future__ import annotations

from typing import Iterable

from bot.config import CFG
from bot.models import (
    Leg,
    Opportunity,
    OrderKind,
    PRIORITY_TAIL_END,
    Side,
)
from bot.scanner import EnrichedMarket, MarketSnapshot
from bot.strategies.base import Strategy


class TailEndStrategy(Strategy):
    name = "tail_end"

    async def generate(self, snap: MarketSnapshot) -> Iterable[Opportunity]:
        opps: list[Opportunity] = []
        for em in snap.tail_end_candidates:
            opp = self._build_opp(em)
            if opp is not None:
                opps.append(opp)
        return opps

    # ------------------------------------------------------------------
    def _build_opp(self, em: EnrichedMarket) -> Opportunity | None:
        days = em.days_to_resolution()
        if days is None or days <= 0 or days > CFG.tail_end_max_days:
            return None
        if em.yes_ask is None or em.no_ask is None:
            return None

        # Pick the leading outcome
        if em.yes_ask >= em.no_ask:
            side_token = em.market.yes_token_id
            price = em.yes_ask
            outcome = "YES"
        else:
            side_token = em.market.no_token_id
            price = em.no_ask
            outcome = "NO"

        if price < CFG.tail_end_min_price:
            return None

        expected_edge = (1.0 - price)  # pays $1 at resolution
        # Confidence scales with how deep the price is and how little time
        # is left — closer to $1 and closer to resolution == higher confidence.
        price_conf = min(1.0, (price - CFG.tail_end_min_price) / 0.07 + 0.85)
        time_conf = 1.0 - (days / CFG.tail_end_max_days) * 0.2
        confidence = round(min(0.99, price_conf * time_conf), 3)

        size = self.size_usdc()
        leg = Leg(
            token_id=side_token,
            side=Side.BUY,
            size_usdc=size,
            kind=OrderKind.LIMIT,
            limit_price=price,
        )
        return Opportunity(
            strategy=self.name,
            market_id=em.market.condition_id,
            market_slug=em.market.slug,
            priority=PRIORITY_TAIL_END,
            confidence=confidence,
            expected_profit_pct=expected_edge,
            legs=[leg],
            reference_price=price,
            max_slippage=CFG.max_slippage,
            metadata={
                "outcome": outcome,
                "days_to_resolution": round(days, 2),
                "stop_loss": CFG.tail_end_stop_loss,
                "entry_price": price,
            },
        )
