"""Strategy G — Orderbook Sniper (deep-discount GTC ladder).

Parks a ladder of resting GTC BUY limit orders at steep discounts on any
liquid market (default prices $0.01 / $0.02 / $0.03 with weights
50% / 30% / 20%). Most of the time these sit unfilled; on a sudden panic
dump or liquidation cascade they fill cheaply and yield asymmetric upside.

UTC-hour multiplier
-------------------
The spec calls for a configurable sizing multiplier by UTC hour band.
Implemented as a simple ``_HOUR_MULTIPLIER`` lookup that you can tune in
code or externalize to the env in a follow-up.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from bot.config import CFG
from bot.models import (
    Leg,
    Opportunity,
    OrderKind,
    PRIORITY_SNIPER,
    Side,
)
from bot.scanner import EnrichedMarket, MarketSnapshot
from bot.strategies.base import Strategy


# Sizing multiplier per UTC hour. Default = 1.0. Boost during US/EU overlap.
_HOUR_MULTIPLIER = {
    **dict.fromkeys(range(0, 6), 0.6),   # late US night / early AU -> quiet
    **dict.fromkeys(range(6, 12), 1.0),  # EU morning
    **dict.fromkeys(range(12, 20), 1.4), # peak US/EU overlap
    **dict.fromkeys(range(20, 24), 1.0),
}


class SniperStrategy(Strategy):
    name = "sniper"

    async def generate(self, snap: MarketSnapshot) -> Iterable[Opportunity]:
        if not CFG.sniper_prices or not CFG.sniper_weights:
            return []
        if len(CFG.sniper_prices) != len(CFG.sniper_weights):
            self.log.warning(
                "SNIPER_PRICES and SNIPER_WEIGHTS length mismatch; disabled."
            )
            return []
        if abs(sum(CFG.sniper_weights) - 1.0) > 0.01:
            self.log.warning("SNIPER_WEIGHTS should sum to 1; normalizing.")

        weights = _normalize(CFG.sniper_weights)
        hour = datetime.now(tz=timezone.utc).hour
        mult = _HOUR_MULTIPLIER.get(hour, 1.0)

        opps: list[Opportunity] = []
        for em in snap.sniper_candidates:
            opp = self._build_opp(em, weights, mult)
            if opp is not None:
                opps.append(opp)
        return opps

    # ------------------------------------------------------------------
    def _build_opp(
        self,
        em: EnrichedMarket,
        weights: list[float],
        hour_mult: float,
    ) -> Opportunity | None:
        # Skip markets whose ask is already below our deepest ladder rung
        # (nothing to snipe — we'd just be taking liquidity at a premium).
        if em.yes_ask is not None and em.yes_ask <= min(CFG.sniper_prices):
            return None

        total_size = self.size_usdc() * hour_mult
        legs: list[Leg] = []
        for price, weight in zip(CFG.sniper_prices, weights):
            size_usdc = round(total_size * weight, 4)
            if size_usdc < 1.0:  # Polymarket min order
                continue
            legs.append(
                Leg(
                    token_id=em.market.yes_token_id,
                    side=Side.BUY,
                    size_usdc=size_usdc,
                    kind=OrderKind.GTC,
                    limit_price=price,
                )
            )
        if not legs:
            return None

        return Opportunity(
            strategy=self.name,
            market_id=em.market.condition_id,
            market_slug=em.market.slug,
            priority=PRIORITY_SNIPER,
            confidence=0.15,  # low hit rate, high asymmetry
            expected_profit_pct=0.50,  # rough estimate when they do fill
            legs=legs,
            reference_price=CFG.sniper_prices[0],
            metadata={
                "hour_mult": hour_mult,
                "ladder": list(zip(CFG.sniper_prices, weights)),
                "current_ask": em.yes_ask,
            },
        )


def _normalize(weights: list[float]) -> list[float]:
    total = sum(weights) or 1.0
    return [w / total for w in weights]
