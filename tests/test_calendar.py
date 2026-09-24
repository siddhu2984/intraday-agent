from datetime import date, time

import pytest

from agent.ops.calendar import CalendarError, NseCalendar, Session
from conftest import make_settings

STRATEGY = make_settings().strategy


def synthetic(**special) -> NseCalendar:
    holidays = {date(2026, 1, 26): "Republic Day", date(2026, 10, 2): "Gandhi Jayanti"}
    return NseCalendar(holidays, {s.day: s for s in special.values()})


# --- rules, on a small synthetic calendar ---

def test_normal_weekday():
    s = synthetic().session(date(2026, 9, 24))
    assert (s.open, s.close, s.kind) == (time(9, 15), time(15, 30), "normal")


def test_weekend_and_holiday_are_closed():
    cal = synthetic()
    assert cal.session(date(2026, 9, 26)) is None       # Saturday
    assert cal.session(date(2026, 10, 2)) is None       # holiday
    assert cal.market_session(date(2026, 10, 2)) is None


def test_full_special_session_is_tradable_even_on_a_weekend_or_holiday():
    budget = Session(date(2026, 2, 1), time(9, 15), time(15, 30), "full")
    cal = synthetic(b=budget)
    assert cal.is_trading_day(date(2026, 2, 1))


@pytest.mark.parametrize("kind", ["special", "muhurat"])
def test_short_special_sessions_are_not_traded(kind):
    muhurat = Session(date(2026, 1, 26), time(18, 0), time(19, 0), kind)
    cal = synthetic(m=muhurat)
    assert cal.market_session(date(2026, 1, 26)).kind == kind  # the exchange is open...
    assert cal.session(date(2026, 1, 26)) is None               # ...the agent is not


def test_year_outside_the_holiday_list_fails_closed():
    with pytest.raises(CalendarError, match="covers 2026"):
        synthetic().session(date(2027, 1, 4))


def test_next_and_previous_trading_day_skip_weekends_and_holidays():
    cal = synthetic()
    assert cal.next_trading_day(date(2026, 10, 1)) == date(2026, 10, 5)      # Fri holiday + weekend
    assert cal.previous_trading_day(date(2026, 10, 5)) == date(2026, 10, 1)
    assert cal.trading_days(date(2026, 9, 28), date(2026, 10, 4)) == [
        date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)]


def test_entry_window():
    cal = synthetic()
    assert cal.entry_window(date(2026, 9, 24), STRATEGY) == (time(9, 30), time(14, 30))
    assert cal.entry_window(date(2026, 9, 26), STRATEGY) is None


def test_entry_window_follows_a_special_sessions_hours():
    short = Session(date(2026, 3, 7), time(10, 0), time(13, 0), "full")
    assert synthetic(s=short).entry_window(date(2026, 3, 7), STRATEGY) == (time(10, 15), time(13, 0))


# --- the real calendar files ---

def test_real_calendar_known_days():
    cal = NseCalendar.load()
    assert cal.session(date(2026, 9, 14)) is None                      # Ganesh Chaturthi
    assert cal.is_trading_day(date(2024, 1, 20))                       # Saturday session
    assert cal.is_trading_day(date(2026, 2, 1))                        # Budget Sunday
    assert cal.market_session(date(2025, 10, 21)).kind == "muhurat"
    assert cal.session(date(2024, 11, 1)) is None                      # missing from NSE's list
    assert cal.session(date(2024, 3, 2)) is None                       # DR-site special session


def test_real_calendar_matches_the_backtest_window():
    # 742 days in the candle store, minus 2 DR-site sessions and 1 afternoon Muhurat session
    assert len(NseCalendar.load().trading_days(date(2023, 9, 25), date(2026, 9, 23))) == 739
