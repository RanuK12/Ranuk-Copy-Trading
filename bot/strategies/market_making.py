"""Strategy F — Maker-rebate market making on crypto 15-min markets.

Places resting GTC limit BUY orders on both YES and NO sides in crypto
15-minute markets whenever the combined bid < ``MM_MAX_TOTAL_PRICE``
(default 0.98). When both legs fill the bot accumulates a complete
YES+NO pair that can be merged back to USDC via the CTF contract —
capturing the spread + any maker rebate.

One-sided stop
--------------
If a previous cycle left one side filled and the other open, this
strategy refuses to re-enter until the imbalance clears. State is
tracked in the shared :class:`StateStore` under key
``mm_one_sided:{market_id}``.

Ghost fill recovery is handled by the executor when it detects a fill
response but no on-chain balance update — out of scope for this pure
strategy module.
"""

from __future__ import annotations

from typing import Iterable

from bot.config import CFG
from bot.models import (
    Leg,
    Opportunity,
    OrderKind,
    PRIORITY_MARKET_MAKING,
    Side,
)
from bot.scanner import EnrichedMarket, MarketSnapshot
from bot.state import get_state
from bot.strategies.base import Strategy


class MarketMakingStrategy(Strategy):
    name = "market_making"

    def __init__(self) -> None:
        super().__init__()
        self._state = get_state()

    async def generate(self, snap: MarketSnapshot) -> Iterable[Opportunity]:
        opps: list[Opportunity] = []
        for em in snap.crypto_15m_markets:
            opp = self._build_opp(em)
            if opp is not None:
                opps.append(opp)
        return opps

    def _build_opp(self, em: EnrichedMarket) -> Opportunity | None:
        if em.yes_bid is None or em.no_bid is None:
            return None
        total_bid = em.yes_bid + em.no_bid
        if total_bid >= CFG.mm_max_total_price:
            return None

        # One-sided stop: refuse re-entry if the previous cycle didn't
        # round-trip (tracked via a dedicated market-scoped position tag).
        tag_key = f"mm_one_sided::{em.market.condition_id}"
        if self._state.has_open_position(self.name, tag_key):
            self.log.debug(
                f"MM: refusing {em.market.slug} — previous cycle one-sided."
            )
            return None

        half = self.size_usdc() / 2.0
        yes_leg = Leg(
            token_id=em.market.yes_token_id,
            side=Side.BUY,
            size_usdc=half,
            kind=OrderKind.GTC,
            limit_price=em.yes_bid,
        )
        no_leg = Leg(
            token_id=em.market.no_token_id,
            side=Side.BUY,
            size_usdc=half,
            kind=OrderKind.GTC,
            limit_price=em.no_bid,
        )
        edge = 1.0 - total_bid
        return Opportunity(
            strategy=self.name,
            market_id=em.market.condition_id,
            market_slug=em.market.slug,
            priority=PRIORITY_MARKET_MAKING,
            confidence=0.70,
            expected_profit_pct=edge,
            legs=[yes_leg, no_leg],
            reference_price=total_bid,
            metadata={
                "yes_bid": em.yes_bid,
                "no_bid": em.no_bid,
                "total_bid": total_bid,
                "ctf_merge_on_both_filled": True,
            },
        )
