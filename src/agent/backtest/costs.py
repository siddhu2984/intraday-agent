"""Intraday equity trading costs (architecture.md §9), from the `costs:` config (FYERS charge sheet).

Per executed order (leg):
  brokerage  min(brokerage_pct of value, brokerage_max)
  STT        stt_sell_pct of value, sell leg only
  exchange   exchange_pct of value (NSE transaction charges)
  SEBI       sebi_pct of value
  stamp      stamp_buy_pct of value, buy leg only
  GST        gst_pct of (brokerage + exchange + SEBI)
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.config import CostsConfig
from agent.risk.state import Side


@dataclass(frozen=True)
class Costs:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst

    def __add__(self, other: Costs) -> Costs:
        return Costs(*(a + b for a, b in zip(self._values(), other._values())))

    def _values(self) -> tuple[float, ...]:
        return self.brokerage, self.stt, self.exchange, self.sebi, self.stamp, self.gst


def leg_costs(value: float, buy: bool, c: CostsConfig) -> Costs:
    brokerage = min(value * c.brokerage_pct / 100, c.brokerage_max)
    exchange = value * c.exchange_pct / 100
    sebi = value * c.sebi_pct / 100
    return Costs(
        brokerage=brokerage,
        stt=0.0 if buy else value * c.stt_sell_pct / 100,
        exchange=exchange,
        sebi=sebi,
        stamp=value * c.stamp_buy_pct / 100 if buy else 0.0,
        gst=(brokerage + exchange + sebi) * c.gst_pct / 100,
    )


def round_trip(side: Side, qty: int, entry: float, exit_: float, c: CostsConfig) -> Costs:
    """A long buys then sells; a short sells then buys."""
    long = side is Side.LONG
    return leg_costs(qty * entry, buy=long, c=c) + leg_costs(qty * exit_, buy=not long, c=c)
