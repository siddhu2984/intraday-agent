"""Order types shared by SimBroker (backtest, paper) and, from phase 3, the live FYERS adapter (architecture.md §4.1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    SL_M = "sl_m"      # stop-loss market: becomes a market order when the trigger trades
    MARKET = "market"


class OrderStatus(str, Enum):
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class OrderRequest:
    tag: str                       # our correlation id: retries look the order up by it (§4.1)
    symbol: str
    action: Action
    qty: int
    order_type: OrderType
    price: float | None = None     # LIMIT
    trigger: float | None = None   # SL_M
    valid_bars: int | None = None  # cancel if unfilled after this many 1-min bars (None = until cancelled)


@dataclass
class Order:
    id: str
    request: OrderRequest
    placed_at: datetime
    status: OrderStatus = OrderStatus.OPEN
    fill_price: float | None = None
    filled_at: datetime | None = None
    bars_seen: int = 0
    placed_mid_bar: bool = False   # placed while a bar was being processed (e.g. a stop right after an entry fill)

    @property
    def tag(self) -> str:
        return self.request.tag

    @property
    def symbol(self) -> str:
        return self.request.symbol


@dataclass(frozen=True)
class Fill:
    order_id: str
    tag: str
    symbol: str
    action: Action
    qty: int
    price: float
    ts: datetime
