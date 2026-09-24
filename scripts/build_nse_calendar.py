"""Build config/calendar/nse_holidays.csv from NSE's holiday list (architecture.md §4.12).

Usage:  python scripts/build_nse_calendar.py

Fetches NSE's capital-market trading holidays from FIRST_YEAR to next year (next year's list appears in December),
then checks the calendar — holidays plus config/calendar/nse_special_sessions.csv — against the candle store:
every stored day must be a session, and every weekday session inside 09:15–15:30 must be stored. Any difference
is an error and nothing is written; fix the special sessions file (NSE's list omits some days, e.g. 2024-11-01).

Upkeep: re-run each December (next year's list) and whenever NSE announces a special holiday or session.
"""

from __future__ import annotations

import datetime as dt
import http.cookiejar
import json
import sys
import time
import urllib.request

import pandas as pd

from agent.data.store import CandleStore
from agent.ops.calendar import CALENDAR_DIR, NORMAL_CLOSE, NORMAL_OPEN, NseCalendar, read_special_sessions

FIRST_YEAR = 2023  # the candle store starts 2023-09-25
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
HOME_URL = "https://www.nseindia.com/resources/exchange-communication-holidays"
API_URL = "https://www.nseindia.com/api/holiday-master?type=trading&year={}"


def fetch_holidays(years: range) -> pd.DataFrame:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [("User-Agent", USER_AGENT), ("Referer", HOME_URL), ("Accept", "application/json")]
    try:
        opener.open(HOME_URL, timeout=30).read()  # sets cookies; the API often works without them
    except Exception:
        pass
    rows = []
    for year in years:
        with opener.open(API_URL.format(year), timeout=30) as response:
            data = json.load(response)
        for r in data.get("CM", []) if isinstance(data, dict) else []:
            day = dt.datetime.strptime(r["tradingDate"], "%d-%b-%Y").date()
            if day.year == year:  # a year NSE hasn't published yet comes back empty or as another year
                rows.append((day.isoformat(), r["description"].strip().rstrip("*").strip()))
        time.sleep(0.5)
    return pd.DataFrame(rows, columns=["date", "description"]).drop_duplicates("date").sort_values("date")


def verify(calendar: NseCalendar, stored: list[dt.date]) -> list[str]:
    stored_set = set(stored)
    problems = []
    day = stored[0]
    while day <= stored[-1]:
        s = calendar.market_session(day)
        # The store keeps only 09:15–15:29 bars, so an evening Muhurat session leaves no day file.
        expected = s is not None and s.open < NORMAL_CLOSE and s.close > NORMAL_OPEN
        if expected and day not in stored_set:
            problems.append(f"{day}: calendar has a {s.kind} session, the candle store has no data")
        if not expected and day in stored_set:
            problems.append(f"{day}: candle store has data, calendar says closed")
        day += dt.timedelta(days=1)
    return problems


def main() -> int:
    today = dt.date.today()
    holidays = fetch_holidays(range(FIRST_YEAR, today.year + 2))
    years = sorted({d[:4] for d in holidays["date"]})
    print(f"{len(holidays)} holidays, years {', '.join(years)}")

    calendar = NseCalendar(
        {dt.date.fromisoformat(d): desc for d, desc in zip(holidays["date"], holidays["description"])},
        read_special_sessions(CALENDAR_DIR / "nse_special_sessions.csv"),
    )
    store = CandleStore()
    stored = store.days()
    store.close()
    problems = verify(calendar, stored)
    if problems:
        sys.exit("calendar does not match the candle store — nothing written:\n  " + "\n  ".join(problems))
    print(f"verified against {len(stored)} stored days ({stored[0]} to {stored[-1]})")

    holidays.to_csv(CALENDAR_DIR / "nse_holidays.csv", index=False)
    print(f"wrote {CALENDAR_DIR / 'nse_holidays.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
