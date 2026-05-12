"""Strategy C — Micro-Spread Farming.

Scalps wide bid/ask spreads on cheap outcomes ($0.05-$0.10). Places a
limit order at the bid, expecting the other side of the book to lift us
at the ask for a ~5-10 cent profit on a 5-10 cent position.

Capital allocation is capped at ``MICRO_CAPITAL_PCT`` of total capital
(via ``RiskManager.adjusted_size``, which also applies the 25% high-risk
scaling to this strategy).
"""

from __future__ import annotations

from typing import Iterable

from bot.config import CFG
from bot.models import (
    Leg,
    Opportunity,
    OrderKind,
    PRIORITY_MICRO_SPREAD,
    Side,
)
from bot.scanner import EnrichedMarket, MarketSnapshot
from bot.strategies.base import Strategy


class MicroSpreadStrategy(Strategy):
    name = "micro_spread"

    async def generate(self, snap: MarketSnapshot) -> Iterable[Opportunity]:
        opps: list[Opportunity] = []
        for em in snap.micro_spread_candidates:
            opp = self._build_opp(em)
            if opp is not None:
                opps.append(opp)
        return opps

    def _build_opp(self, em: EnrichedMarket) -> Opportunity | None:
        if em.yes_bid is None or em.yes_ask is None:
            return None
        spread = em.yes_ask - em.yes_bid
        if spread < CFG.micro_min_spread or em.yes_bid < CFG.micro_price_min:
            return None

        # Buy at (or just above) the bid; we'll exit at the ask later via
        # a cancel/replace cycle managed by the operator or a future TODO.
        entry_price = round(em.yes_bid + 0.001, 4)
        edge = spread / max(entry_price, 1e-6)
        if edge < 0.15:  # require at least 15% round-trip edge
            return None

        size = self.size_usdc()
        leg = Leg(
            token_id=em.market.yes_token_id,
            side=Side.BUY,
            size_usdc=size,
            kind=OrderKind.GTC,  # resting order; other side of book picks it off
            limit_price=entry_price,
        )
        return Opportunity(
            strategy=self.name,
            market_id=em.market.condition_id,
            market_slug=em.market.slug,
            priority=PRIORITY_MICRO_SPREAD,
            confidence=0.55,
            expected_profit_pct=edge,
            legs=[leg],
            reference_price=entry_price,
            max_slippage=None,  # GTC limit, no need for slippage check
            metadata={
                "bid": em.yes_bid,
                "ask": em.yes_ask,
                "spread": spread,
                "volume_usdc": em.market.volume_usdc,
            },
        )
