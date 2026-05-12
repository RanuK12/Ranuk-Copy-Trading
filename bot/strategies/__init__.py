"""Trading strategy implementations.

Each module exposes a subclass of :class:`bot.strategies.base.Strategy`
and registers itself in :data:`REGISTRY`.
"""

from bot.strategies.base import Strategy  # noqa: F401
from bot.strategies import (  # noqa: F401
    arbitrage,
    tail_end,
    micro_spread,
    dip_arb,
    smart_copy,
    market_making,
    sniper,
)

REGISTRY: dict[str, type[Strategy]] = {
    arbitrage.ArbitrageStrategy.name: arbitrage.ArbitrageStrategy,
    tail_end.TailEndStrategy.name: tail_end.TailEndStrategy,
    micro_spread.MicroSpreadStrategy.name: micro_spread.MicroSpreadStrategy,
    dip_arb.DipArbStrategy.name: dip_arb.DipArbStrategy,
    smart_copy.SmartCopyStrategy.name: smart_copy.SmartCopyStrategy,
    market_making.MarketMakingStrategy.name: market_making.MarketMakingStrategy,
    sniper.SniperStrategy.name: sniper.SniperStrategy,
}
