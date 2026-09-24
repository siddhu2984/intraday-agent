"""NSE trading calendar (architecture.md §4.12).

config/calendar/
  nse_holidays.csv          date, description — NSE's trading holidays (scripts/build_nse_calendar.py)
  nse_special_sessions.csv  date, kind, open, close, note — hand-curated sessions that differ from normal:
                            full     a regular session on a weekend/holiday (e.g. Budget day) — the agent trades
                            special  a short special session (e.g. live trading from the DR site) — no trading
                            muhurat  the Diwali Muhurat session — no trading

Rule order for a day: special session file → holiday file / weekend → normal 09:15–15:30.
A day outside the years the holiday file covers raises CalendarError, so a stale file can't pass as "no holidays".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from agent.broker.fyers_auth import PROJECT_ROOT
from agent.config import StrategyConfig

CALENDAR_DIR = PROJECT_ROOT / "config" / "calendar"
NORMAL_OPEN, NORMAL_CLOSE = time(9, 15), time(15, 30)
SPECIAL_KINDS = ("full", "special", "muhurat")


class CalendarError(RuntimeError):
    pass


@dataclass(frozen=True)
class Session:
    day: date
    open: time
    close: time
    kind: str  # 'normal' or one of SPECIAL_KINDS
    note: str = ""

    @property
    def tradable(self) -> bool:
        return self.kind in ("normal", "full")


def _add_minutes(t: time, minutes: int) -> time:
    return (datetime.combine(date.min, t) + timedelta(minutes=minutes)).time()


def read_holidays(path: Path) -> dict[date, str]:
    df = pd.read_csv(path, dtype=str)
    return {date.fromisoformat(d): desc for d, desc in zip(df["date"], df["description"])}


def read_special_sessions(path: Path) -> dict[date, Session]:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    special = {}
    for row in df.itertuples():
        if row.kind not in SPECIAL_KINDS:
            raise CalendarError(f"special session {row.date}: unknown kind {row.kind!r}")
        day = date.fromisoformat(row.date)
        special[day] = Session(day, time.fromisoformat(row.open), time.fromisoformat(row.close), row.kind, row.note)
    return special


class NseCalendar:
    def __init__(self, holidays: dict[date, str], special: dict[date, Session]):
        if not holidays:
            raise CalendarError("no holidays loaded")
        self.holidays = holidays
        self.special = special
        self.years = range(min(holidays).year, max(holidays).year + 1)

    @classmethod
    def load(cls, directory: Path = CALENDAR_DIR) -> NseCalendar:
        return cls(read_holidays(directory / "nse_holidays.csv"),
                   read_special_sessions(directory / "nse_special_sessions.csv"))

    def market_session(self, day: date) -> Session | None:
        """The exchange's session that day, of any kind (None = market closed)."""
        if day.year not in self.years:
            raise CalendarError(f"{day}: holiday list covers {self.years.start}–{self.years.stop - 1} only; "
                                "run scripts/build_nse_calendar.py")
        if day in self.special:
            return self.special[day]
        if day.weekday() >= 5 or day in self.holidays:
            return None
        return Session(day, NORMAL_OPEN, NORMAL_CLOSE, "normal")

    def session(self, day: date) -> Session | None:
        """The session the agent trades (None = no trading: closed, or a special/Muhurat session)."""
        s = self.market_session(day)
        return s if s and s.tradable else None

    def is_trading_day(self, day: date) -> bool:
        return self.session(day) is not None

    def next_trading_day(self, day: date) -> date:
        day += timedelta(days=1)
        while not self.is_trading_day(day):
            day += timedelta(days=1)
        return day

    def previous_trading_day(self, day: date) -> date:
        day -= timedelta(days=1)
        while not self.is_trading_day(day):
            day -= timedelta(days=1)
        return day

    def trading_days(self, start: date, end: date) -> list[date]:
        """Trading days with start <= day <= end."""
        return [start + timedelta(days=i) for i in range((end - start).days + 1)
                if self.is_trading_day(start + timedelta(days=i))]

    def entry_window(self, day: date, strategy: StrategyConfig) -> tuple[time, time] | None:
        """[first, last) time new entries are allowed: after the opening range, before last_entry."""
        s = self.session(day)
        if s is None:
            return None
        return _add_minutes(s.open, strategy.opening_range_min), min(strategy.last_entry, s.close)
