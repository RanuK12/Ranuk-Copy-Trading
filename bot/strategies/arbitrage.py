"""Strategy A — Sum-to-One Arbitrage.

Identifies binary markets where the ask prices of YES and NO sum to less
than $1, emits a two-leg FOK buy opportunity. Maximum priority because
these opportunities disappear in seconds.

Profit per share  = 1 - (yes_ask + no_ask)    [USDC]
Expected edge     = profit / 1                [fraction of notional]
"""

from __future__ import annotations

from typing import Iterable

from bot.config import CFG
from bot.models import (
    Leg,
    Opportunity,
    OrderKind,
    PRIORITY_ARBITRAGE,
    Side,
)
from bot.scanner import EnrichedMarket, MarketSnapshot
from bot.strategies.base import Strategy


class ArbitrageStrategy(Strategy):
    name = "arbitrage"

    async def generate(self, snap: MarketSnapshot) -> Iterable[Opportunity]:
        opps: list[Opportunity] = []
        for em in snap.arbitrage_candidates:
            opp = self._build_opp(em)
            if opp is not None:
                opps.append(opp)
        if opps:
            self.log.info(
                f"[green]{len(opps)} sum-to-one arbitrage candidate(s)[/]"
            )
        return opps

    # ------------------------------------------------------------------
    def _build_opp(self, em: EnrichedMarket) -> Opportunity | None:
        if em.yes_ask is None or em.no_ask is None:
            return None
        total = em.yes_ask + em.no_ask
        edge = 1.0 - total
        if edge < CFG.arb_min_profit:
            return None

        # Split notional evenly between YES and NO legs so the combined
        # position is fully hedged (1 share of each outcome pays $1).
        half = self.size_usdc() / 2.0
        legs = [
            Leg(
                token_id=em.market.yes_token_id,
                side=Side.BUY,
                size_usdc=half,
                kind=OrderKind.FOK,
                limit_price=em.yes_ask,
            ),
            Leg(
                token_id=em.market.no_token_id,
                side=Side.BUY,
                size_usdc=half,
                kind=OrderKind.FOK,
                limit_price=em.no_ask,
            ),
        ]
        return Opportunity(
            strategy=self.name,
            market_id=em.market.condition_id,
            market_slug=em.market.slug,
            priority=PRIORITY_ARBITRAGE,
            confidence=0.98,  # math-guaranteed; only execution risk remains
            expected_profit_pct=edge,
            legs=legs,
            reference_price=total,
            metadata={
                "yes_ask": em.yes_ask,
                "no_ask": em.no_ask,
                "total_ask": total,
                "edge": edge,
                "volume_usdc": em.market.volume_usdc,
            },
        )
