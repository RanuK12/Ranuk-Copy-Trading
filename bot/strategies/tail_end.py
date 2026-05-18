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
* risk/reward: (1 - entry) / (entry - stop_loss) >= min_ratio
* reject in-progress sports events (wide spread + short window)
* stop-loss threshold set on the Opportunity for monitoring
"""

from __future__ import annotations

from typing import Iterable

from bot.config import CFG
from bot.intelligence import is_live_sports_event, risk_adjusted_edge_ok
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
        from bot.logger import get_logger
        log = get_logger("tail_end")
        opps: list[Opportunity] = []
        log.info(f"tail_end candidates: {len(snap.tail_end_candidates)}")
        for em in snap.tail_end_candidates:
            opp = self._build_opp(em)
            if opp is not None:
                opps.append(opp)
                log.info(f"tail_end opp: {em.market.slug[:40]} price={opp.legs[0].limit_price:.3f} edge={opp.expected_profit_pct:.3f}")
            else:
                days = em.days_to_resolution()
                yes = em.yes_ask if em.yes_ask is not None else -1
                no = em.no_ask if em.no_ask is not None else -1
                log.info(f"tail_end skip: {em.market.slug[:40]} yes={yes:.3f} no={no:.3f} days={days}")
        if not opps:
            log.info("tail_end: no opps generated")
        return opps

    # ------------------------------------------------------------------
    def _build_opp(self, em: EnrichedMarket) -> Opportunity | None:
        from bot.logger import get_logger
        log = get_logger("tail_end")

        days = em.days_to_resolution()
        if days is None or days <= 0 or days > CFG.tail_end_max_days:
            return None
        if em.yes_ask is None or em.no_ask is None:
            return None

        # Reject in-progress sports: they look "tail-ish" (one side close
        # to 1.0) but the game might flip in the next play.
        is_live, reason = is_live_sports_event(
            slug=em.market.slug,
            question=em.market.question,
            end_date=em.market.end_date,
            yes_ask=em.yes_ask,
            yes_bid=em.yes_bid,
            no_ask=em.no_ask,
            live_window_hours=6.0,
        )
        if is_live:
            log.info(f"tail_end skip (live event): {em.market.slug[:40]} {reason}")
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
        if expected_edge < CFG.tail_end_min_edge:
            return None

        # Risk-adjusted edge check: compare upside (1 - price) against the
        # actual stop-loss distance that position_monitor will enforce
        # (``stop_loss_pct * price``). This keeps the R/R math aligned
        # with what actually happens at runtime.
        dynamic_sl = price * (1 - CFG.stop_loss_pct)
        effective_sl = max(dynamic_sl, CFG.tail_end_stop_loss)
        rr_ok, rr_reason = risk_adjusted_edge_ok(
            entry_price=price,
            stop_loss_price=effective_sl,
            min_ratio=CFG.tail_end_min_rr_ratio,
        )
        if not rr_ok:
            log.info(f"tail_end skip (risk/reward): {em.market.slug[:40]} {rr_reason}")
            return None

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
                "rr_ratio": rr_reason,
            },
        )
