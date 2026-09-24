"""The conservative 1-min fill model (agent.broker.sim)."""

from datetime import datetime, timedelta

import pytest

from agent.broker.base import Action, OrderRequest, OrderStatus, OrderType
from agent.broker.fyers_auth import IST
from agent.broker.sim import Bar, SimBroker

T0 = datetime(2026, 9, 24, 10, 0, tzinfo=IST)
SYM = "NSE:INFY-EQ"


def bar(o, h, l, c, minute=0):
    return Bar(T0 + timedelta(minutes=minute), o, h, l, c, 1000)


def order(tag, action, order_type, price=None, trigger=None, valid_bars=None, qty=10):
    return OrderRequest(tag, SYM, action, qty, order_type, price, trigger, valid_bars)


@pytest.fixture
def broker():
    return SimBroker(slippage_bps=10)  # 0.1% to make slippage visible


# --- limits ---

@pytest.mark.parametrize("action, limit, b, expected", [
    (Action.BUY, 100.0, bar(99.5, 100.5, 99.0, 100.2), 99.5),     # open better than limit → open
    (Action.BUY, 100.0, bar(100.5, 101, 99.9, 100.8), 100.0),     # trades through → limit
    (Action.BUY, 100.0, bar(100.5, 101, 100.0, 100.8), None),     # only touches → no fill
    (Action.SELL, 100.0, bar(100.5, 101, 99.5, 100.2), 100.5),
    (Action.SELL, 100.0, bar(99.5, 100.1, 99.0, 99.8), 100.0),
    (Action.SELL, 100.0, bar(99.5, 100.0, 99.0, 99.8), None),
])
def test_limit_fills(broker, action, limit, b, expected):
    oid = broker.place(order("e", action, OrderType.LIMIT, price=limit), T0)
    fills = broker.process_bar(SYM, b)
    assert (fills[0].price if fills else None) == expected
    assert broker.orders[oid].status is (OrderStatus.FILLED if expected else OrderStatus.OPEN)


def test_entry_expires_after_its_bars(broker):
    oid = broker.place(order("e", Action.BUY, OrderType.LIMIT, price=100, valid_bars=1), T0)
    assert broker.process_bar(SYM, bar(101, 102, 100.5, 101.5)) == []
    assert broker.orders[oid].status is OrderStatus.CANCELLED


def test_orders_placed_for_a_later_time_ignore_earlier_bars(broker):
    broker.place(order("e", Action.BUY, OrderType.LIMIT, price=100), T0 + timedelta(minutes=5))
    assert broker.process_bar(SYM, bar(99, 99.5, 98, 99)) == []


# --- stops (SL-M) ---

@pytest.mark.parametrize("action, trigger, b, expected", [
    (Action.SELL, 95.0, bar(96, 96.5, 94.5, 95.5), 95.0 * 0.999),   # touched → trigger − slippage
    (Action.SELL, 95.0, bar(96, 96.5, 95.0, 95.5), 95.0 * 0.999),   # touch is enough for a stop
    (Action.SELL, 95.0, bar(94, 94.5, 93, 94), 94.0 * 0.999),       # gapped through → open − slippage
    (Action.SELL, 95.0, bar(96, 96.5, 95.1, 95.5), None),
    (Action.BUY, 105.0, bar(104, 105.5, 103.5, 104), 105.0 * 1.001),  # short's stop: slippage upward
    (Action.BUY, 105.0, bar(106, 107, 105.5, 106), 106.0 * 1.001),
])
def test_stop_fills(broker, action, trigger, b, expected):
    broker.place(order("s", action, OrderType.SL_M, trigger=trigger), T0)
    fills = broker.process_bar(SYM, b)
    assert (fills[0].price if fills else None) == (pytest.approx(expected) if expected else None)


def test_market_fills_at_the_next_open_with_slippage(broker):
    broker.place(order("m", Action.SELL, OrderType.MARKET), T0)
    [f] = broker.process_bar(SYM, bar(100, 101, 99, 100.5))
    assert f.price == pytest.approx(99.9)


# --- stop and target together ---

def bracket(broker, stop=95.0, target=110.0):
    """A long's exits, cancelling each other like the trade manager does."""
    ids = {}

    def on_fill(fill):
        other = ids["target"] if fill.order_id == ids["stop"] else ids["stop"]
        broker.cancel(other)

    ids["stop"] = broker.place(order("s", Action.SELL, OrderType.SL_M, trigger=stop), T0)
    ids["target"] = broker.place(order("t", Action.SELL, OrderType.LIMIT, price=target), T0)
    return ids, on_fill


def test_bar_touching_stop_and_target_is_a_loss(broker):
    ids, on_fill = bracket(broker)
    [f] = broker.process_bar(SYM, bar(100, 111, 94, 105), on_fill=on_fill)
    assert f.order_id == ids["stop"]
    assert broker.orders[ids["target"]].status is OrderStatus.CANCELLED


def test_gap_through_target_at_the_open_wins(broker):
    ids, on_fill = bracket(broker)
    [f] = broker.process_bar(SYM, bar(112, 113, 94, 100), on_fill=on_fill)
    assert (f.order_id, f.price) == (ids["target"], 112)


def test_stop_placed_after_an_entry_fill_can_be_hit_in_the_same_bar(broker):
    placed = {}

    def on_fill(fill):
        if fill.tag == "e":
            placed["stop"] = broker.place(order("s", Action.SELL, OrderType.SL_M, trigger=99.0), fill.ts)
            placed["target"] = broker.place(order("t", Action.SELL, OrderType.LIMIT, price=101.0), fill.ts)

    broker.place(order("e", Action.BUY, OrderType.LIMIT, price=100.0), T0)
    fills = broker.process_bar(SYM, bar(100.0, 102, 98.5, 99.5), on_fill=on_fill)
    assert [f.tag for f in fills] == ["e", "s"]                      # entry at the open, then stopped
    assert broker.orders[placed["target"]].status is OrderStatus.OPEN  # the target waits for the next bar


def test_modify_moves_a_stop(broker):
    oid = broker.place(order("s", Action.SELL, OrderType.SL_M, trigger=95.0), T0)
    broker.modify(oid, trigger=100.0)
    [f] = broker.process_bar(SYM, bar(101, 101.5, 99.8, 100))
    assert f.price == pytest.approx(100 * 0.999)


def test_validation(broker):
    with pytest.raises(ValueError):
        broker.place(order("x", Action.BUY, OrderType.LIMIT), T0)
    with pytest.raises(ValueError):
        broker.place(order("x", Action.BUY, OrderType.SL_M), T0)
    broker.place(order("dup", Action.BUY, OrderType.MARKET), T0)
    with pytest.raises(ValueError, match="duplicate tag"):
        broker.place(order("dup", Action.BUY, OrderType.MARKET), T0)
    oid = broker.by_tag("dup").id
    broker.cancel(oid)
    with pytest.raises(ValueError):
        broker.modify(oid, price=1)
