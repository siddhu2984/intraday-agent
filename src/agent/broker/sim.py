"""Simulated broker: fills orders against 1-min bars (architecture.md §4.1, §9).

The fill model is deliberately conservative, because a 1-min bar doesn't say in which order its prices traded:

  At the open    Any order whose condition already holds at the bar's open fills at the open (a limit gets the
                 better price; a stop that was gapped through fills at the open, minus slippage).
  Inside the bar Stops are checked before limits, so a bar that touches both the stop and the target is a loss.
                 A limit fills only if price trades *through* it (touching is not enough); a stop triggers on touch.
  Mid-bar orders An order placed while a bar is processed (a stop placed right after its entry filled) can only
                 be hit by that bar's range if it is a stop; limits and market orders wait for the next bar.
  Slippage       Stop and market fills are moved `slippage_bps` against us. Limit fills get their price.

No partial fills. `on_fill` is called for each fill as it happens, and may place, modify or cancel orders
(e.g. cancel the target once the stop has filled) — the rest of the bar respects those changes.
"""

from __future__ import annotations

import itertools
from datetime import datetime
from typing import Callable, NamedTuple

from agent.broker.base import Action, Fill, Order, OrderRequest, OrderStatus, OrderType


class Bar(NamedTuple):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0


class SimBroker:
    def __init__(self, slippage_bps: float):
        self.slippage = slippage_bps / 10_000
        self.orders: dict[str, Order] = {}
        self._ids = itertools.count(1)
        self._in_bar: str | None = None  # symbol whose bar is being processed

    # --- order entry ---

    def place(self, request: OrderRequest, ts: datetime) -> str:
        if request.qty < 1:
            raise ValueError("qty must be >= 1")
        if request.order_type is OrderType.LIMIT and request.price is None:
            raise ValueError("a LIMIT order needs a price")
        if request.order_type is OrderType.SL_M and request.trigger is None:
            raise ValueError("an SL_M order needs a trigger")
        if any(o.tag == request.tag for o in self.orders.values()):
            raise ValueError(f"duplicate tag {request.tag}")
        order_id = f"SIM{next(self._ids)}"
        self.orders[order_id] = Order(order_id, request, ts, placed_mid_bar=self._in_bar == request.symbol)
        return order_id

    def modify(self, order_id: str, *, price: float | None = None, trigger: float | None = None) -> None:
        order = self._open(order_id)
        r = order.request
        order.request = OrderRequest(r.tag, r.symbol, r.action, r.qty, r.order_type,
                                     price if price is not None else r.price,
                                     trigger if trigger is not None else r.trigger, r.valid_bars)

    def cancel(self, order_id: str) -> None:
        order = self.orders[order_id]
        if order.status is OrderStatus.OPEN:
            order.status = OrderStatus.CANCELLED

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self.orders.values()
                if o.status is OrderStatus.OPEN and (symbol is None or o.symbol == symbol)]

    def by_tag(self, tag: str) -> Order | None:
        return next((o for o in self.orders.values() if o.tag == tag), None)

    def _open(self, order_id: str) -> Order:
        order = self.orders[order_id]
        if order.status is not OrderStatus.OPEN:
            raise ValueError(f"order {order_id} is {order.status.value}")
        return order

    # --- matching ---

    def _slipped(self, price: float, action: Action) -> float:
        return price * (1 + self.slippage) if action is Action.BUY else price * (1 - self.slippage)

    def _at_open(self, order: Order, bar: Bar) -> float | None:
        r, o = order.request, bar.open
        if r.order_type is OrderType.MARKET:
            return self._slipped(o, r.action)
        if r.order_type is OrderType.LIMIT:
            hit = o <= r.price if r.action is Action.BUY else o >= r.price
            return o if hit else None
        hit = o >= r.trigger if r.action is Action.BUY else o <= r.trigger
        return self._slipped(o, r.action) if hit else None

    def _stop_in_bar(self, order: Order, bar: Bar) -> float | None:
        r = order.request
        hit = bar.high >= r.trigger if r.action is Action.BUY else bar.low <= r.trigger
        return self._slipped(r.trigger, r.action) if hit else None

    def _limit_in_bar(self, order: Order, bar: Bar) -> float | None:
        r = order.request
        hit = bar.low < r.price if r.action is Action.BUY else bar.high > r.price
        return r.price if hit else None

    def process_bar(self, symbol: str, bar: Bar, on_fill: Callable[[Fill], None] | None = None) -> list[Fill]:
        """Match this symbol's open orders against one 1-min bar."""
        fills: list[Fill] = []

        def fill(order: Order, price: float) -> None:
            order.status, order.fill_price, order.filled_at = OrderStatus.FILLED, price, bar.ts
            r = order.request
            f = Fill(order.id, r.tag, r.symbol, r.action, r.qty, price, bar.ts)
            fills.append(f)
            if on_fill:
                on_fill(f)

        def live(order: Order) -> bool:
            return order.status is OrderStatus.OPEN

        self._in_bar = symbol
        try:
            resting = [o for o in self.open_orders(symbol) if o.placed_at <= bar.ts]
            resting_ids = {o.id for o in resting}
            stops = [o for o in resting if o.request.order_type is OrderType.SL_M]
            limits = [o for o in resting if o.request.order_type is OrderType.LIMIT]
            for orders, match in ((resting, self._at_open), (stops, self._stop_in_bar), (limits, self._limit_in_bar)):
                for o in orders:
                    if live(o) and (price := match(o, bar)) is not None:
                        fill(o, price)
            # Stops placed during this bar (after an entry fill) can still be hit by its range.
            for o in self.open_orders(symbol):
                if o.placed_mid_bar and o.id not in resting_ids and o.request.order_type is OrderType.SL_M and live(o):
                    if (price := self._stop_in_bar(o, bar)) is not None:
                        fill(o, price)
        finally:
            self._in_bar = None

        for o in resting:
            o.bars_seen += 1
            if live(o) and o.request.valid_bars is not None and o.bars_seen >= o.request.valid_bars:
                o.status = OrderStatus.CANCELLED
        for o in self.open_orders(symbol):
            o.placed_mid_bar = False
        return fills
