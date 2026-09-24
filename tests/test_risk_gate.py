"""Every §4.7 check: a baseline that is approved, then one change per check that must be rejected by that check."""

from dataclasses import replace
from datetime import date, datetime, time

import pytest

from agent.broker.fyers_auth import IST
from agent.ops.calendar import NseCalendar
from agent.risk.gate import breach, check_entry, check_exit
from agent.risk.state import DayCounters, EntryIntent, ExitIntent, OpenPosition, RiskState, Side
from conftest import make_settings

SETTINGS = make_settings()
CALENDAR = NseCalendar({date(2026, 1, 26): "Republic Day"}, {})
DAY = date(2026, 9, 24)  # a Thursday


def at(hh, mm, ss=0, day=DAY):
    return datetime.combine(day, time(hh, mm, ss), IST)


INTENT = EntryIntent("NSE:INFY-EQ", "Information Technology", Side.LONG, entry=1000, stop=990,
                     upper_circuit=1100, lower_circuit=900)
STATE = RiskState(now=at(10, 0), start_equity=100_000, available_funds=100_000, realized_pnl=0, unrealized_pnl=0,
                  consecutive_losses=0, trades_today=0)


def pos(symbol, sector, side=Side.LONG, qty=10):
    return OpenPosition(symbol, sector, side, qty, entry=500, stop=495)


def failed(intent=INTENT, state=STATE, settings=SETTINGS):
    return check_entry(intent, state, settings, CALENDAR).failed


def test_baseline_is_approved_with_the_cap_binding():
    d = check_entry(INTENT, STATE, SETTINGS, CALENDAR)
    assert d.approved and d.reason == "approved"
    assert d.qty == 20 and d.sizing.binding == "position_cap"
    assert len(d.checks) == 14 and not d.failed


@pytest.mark.parametrize("change, check", [
    ({"kill_switch": True}, "kill_switch"),
    ({"data_healthy": False}, "data_healthy"),
    ({"reconcile_ok": False}, "reconcile_ok"),
    ({"now": at(9, 29, 59)}, "entry_window"),                       # opening range not yet complete
    ({"now": at(14, 30)}, "entry_window"),                          # last_entry is exclusive
    ({"now": at(8, 0)}, "entry_window"),
    ({"now": at(10, 0, day=date(2026, 9, 26))}, "entry_window"),    # Saturday
    ({"now": at(10, 0, day=date(2026, 1, 26))}, "entry_window"),    # holiday
    ({"realized_pnl": -2000}, "daily_loss"),                        # exactly at -2%
    ({"realized_pnl": -1200, "unrealized_pnl": -800}, "daily_loss"),  # unrealized counts
    ({"consecutive_losses": 3}, "consecutive_losses"),
    ({"trades_today": 5}, "trades_per_day"),
    ({"positions": (pos("A", "Banks"), pos("B", "Power"), pos("C", "Metals"))}, "open_positions"),
    ({"positions": (pos("NSE:INFY-EQ", "Banks"),)}, "symbol_not_held"),
    ({"positions": (pos("NSE:TCS-EQ", "Information Technology"),)}, "sector"),
    ({"available_funds": 24_000}, "margin"),                        # 20,000 needed > 80% of 24,000
])
def test_each_state_limit_rejects(change, check):
    assert failed(state=replace(STATE, **change)) == [check]


@pytest.mark.parametrize("change", [
    {"now": at(9, 30)},                          # first second of the window
    {"now": at(14, 29, 59)},
    {"realized_pnl": -1999.99},
    {"realized_pnl": 5000, "unrealized_pnl": -6999},
    {"consecutive_losses": 2},
    {"trades_today": 4},
    {"positions": (pos("A", "Banks"), pos("B", "Power"))},
    {"available_funds": 25_000},                 # 20,000 needed == 80% of 25,000
])
def test_just_inside_each_limit_is_approved(change):
    assert failed(state=replace(STATE, **change)) == []


@pytest.mark.parametrize("change, checks", [
    ({"sector": None}, ["sector"]),
    ({"stop": 1010}, ["stop", "quantity"]),                        # long with stop above entry
    ({"side": Side.SHORT}, ["stop", "quantity"]),                  # short with stop below entry
    ({"stop": 1000}, ["stop", "quantity"]),
    ({"stop": 997.1}, ["stop", "quantity"]),                       # 0.29% < min_stop_pct 0.3%
    ({"entry": 25_000, "stop": 24_900, "upper_circuit": 27_500, "lower_circuit": 22_500}, ["quantity"]),
    ({"upper_circuit": None}, ["circuit"]),
    ({"lower_circuit": None}, ["circuit"]),
    ({"upper_circuit": 1009.9}, ["circuit"]),                      # 0.99% below the upper circuit
    ({"lower_circuit": 990.1}, ["circuit"]),                       # 0.99% above the lower circuit
])
def test_each_intent_problem_rejects(change, checks):
    assert failed(intent=replace(INTENT, **change)) == checks


@pytest.mark.parametrize("change", [
    {"stop": 997},                                    # exactly min_stop_pct
    {"upper_circuit": 1010, "lower_circuit": 990},    # exactly circuit_buffer_pct from both
    {"side": Side.SHORT, "stop": 1010},
])
def test_intent_boundaries_are_approved(change):
    assert failed(intent=replace(INTENT, **change)) == []


def test_all_failures_are_reported_and_the_first_is_the_reason():
    state = replace(STATE, kill_switch=True, trades_today=5)
    d = check_entry(replace(INTENT, sector=None), state, SETTINGS, CALENDAR)
    assert not d.approved and d.qty == 0
    assert d.failed == ["kill_switch", "trades_per_day", "sector"]
    assert d.reason == "kill switch engaged"


def test_leverage_lets_risk_decide_the_size():
    d = check_entry(INTENT, STATE, make_settings(risk={"leverage": 5}), CALENDAR)
    assert d.approved and d.qty == 50 and d.sizing.binding == "risk"
    assert d.sizing.risk_amount == pytest.approx(500)


def test_margin_uses_leverage():
    # 50 shares × 1000 / 5 = 10,000 margin; 80% of 12,000 = 9,600
    state = replace(STATE, available_funds=12_000)
    assert failed(state=state, settings=make_settings(risk={"leverage": 5})) == ["margin"]


def test_sizing_uses_start_of_day_equity_not_pnl():
    d = check_entry(INTENT, replace(STATE, realized_pnl=-1500), SETTINGS, CALENDAR)
    assert d.qty == 20


def test_utc_times_are_converted_to_ist():
    assert failed(state=replace(STATE, now=datetime.fromisoformat("2026-09-24T04:30:00+00:00"))) == []  # 10:00 IST
    assert failed(state=replace(STATE, now=datetime.fromisoformat("2026-09-24T09:00:00+00:00"))) == ["entry_window"]


# --- exits ---

HELD = replace(STATE, positions=(pos("NSE:INFY-EQ", "Information Technology", qty=20),))


@pytest.mark.parametrize("state_change", [{}, {"kill_switch": True}, {"now": at(15, 10)}, {"data_healthy": False}])
def test_exit_that_reduces_a_position_is_always_allowed(state_change):
    d = check_exit(ExitIntent("NSE:INFY-EQ", Side.LONG, 20), replace(HELD, **state_change))
    assert d.approved and d.qty == 20


@pytest.mark.parametrize("intent, reason", [
    (ExitIntent("NSE:TCS-EQ", Side.LONG, 5), "no open position"),
    (ExitIntent("NSE:INFY-EQ", Side.SHORT, 5), "is long"),
    (ExitIntent("NSE:INFY-EQ", Side.LONG, 21), "not in 1..20"),
    (ExitIntent("NSE:INFY-EQ", Side.LONG, 0), "not in 1..20"),
])
def test_exit_that_would_add_exposure_is_rejected(intent, reason):
    d = check_exit(intent, HELD)
    assert not d.approved and d.qty == 0 and reason in d.reason


# --- kill-switch triggers and day counters ---

@pytest.mark.parametrize("change, expected", [
    ({}, None),
    ({"realized_pnl": -1999}, None),
    ({"realized_pnl": -2000}, "daily loss"),
    ({"realized_pnl": -500, "unrealized_pnl": -1600}, "daily loss"),
    ({"consecutive_losses": 2}, None),
    ({"consecutive_losses": 3}, "consecutive losses"),
])
def test_breach(change, expected):
    reason = breach(replace(STATE, **change), SETTINGS)
    assert reason is None if expected is None else expected in reason


def test_day_counters():
    c = DayCounters(DAY)
    for pnl in (-100, -50, 0, -10, -20, 300, -5):
        c.record_entry()
        c.record_close(pnl)
    assert c.trades == 7
    assert c.consecutive_losses == 1          # the breakeven (0) and the win each reset the streak
    assert c.realized_pnl == pytest.approx(115)
