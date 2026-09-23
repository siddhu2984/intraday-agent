from datetime import date, datetime, time

import pandas as pd
import pytest

from agent.broker.fyers_auth import IST
from agent.broker.ratelimit import RateLimiter
from agent.data.download import build_month, chunks, last_final_session, months
from agent.data.quality import SESSION_BARS, check, clean
from agent.data.store import CandleStore, candles_to_frame, chunk_range

DAY = date(2026, 9, 9)


def session_candles(day: date, price: float = 100.0, volume: int = 10, start=time(9, 15), bars=SESSION_BARS):
    first = int(datetime.combine(day, start, IST).timestamp())
    return [[first + 60 * i, price, price + 1, price - 1, price, volume] for i in range(bars)]


def frame(symbol="NSE:SBIN-EQ", day=DAY, **kwargs):
    return candles_to_frame(symbol, session_candles(day, **kwargs))


# --- chunking ---

def test_chunk_range():
    assert chunk_range("2025Q1") == (date(2025, 1, 1), date(2025, 3, 31))
    assert chunk_range("2025Q4") == (date(2025, 10, 1), date(2025, 12, 31))
    assert chunk_range("2024") == (date(2024, 1, 1), date(2024, 12, 31))


def test_quarter_chunks_clip_to_end_and_mark_the_open_quarter_incomplete():
    result = chunks("1m", date(2025, 8, 15), date(2026, 2, 10))
    assert [c.name for c in result] == ["2025Q3", "2025Q4", "2026Q1"]
    assert result[0].start == date(2025, 7, 1)  # whole quarters, so chunk files are canonical
    assert result[-1].end == date(2026, 2, 10)
    assert [c.complete for c in result] == [True, True, False]
    assert all((c.end - c.start).days < 100 for c in result)  # FYERS 1-min request limit


def test_year_chunks():
    assert [c.name for c in chunks("1d", date(2024, 3, 1), date(2026, 9, 23))] == ["2024", "2025", "2026"]


def test_months_split_at_calendar_boundaries():
    assert months(date(2026, 1, 20), date(2026, 3, 5)) == [
        (date(2026, 1, 20), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 5)),
    ]


def test_last_final_session():
    assert last_final_session(datetime(2026, 9, 23, 15, 44, tzinfo=IST)) == date(2026, 9, 22)
    assert last_final_session(datetime(2026, 9, 23, 15, 45, tzinfo=IST)) == date(2026, 9, 23)


# --- cleaning and checks ---

def test_candles_to_frame_uses_ist_timestamps():
    df = frame()
    assert list(df.columns) == ["symbol", "ts", "open", "high", "low", "close", "volume"]
    assert df["ts"].iloc[0] == pd.Timestamp("2026-09-09 09:15", tz="Asia/Kolkata")


def test_clean_drops_pre_open_bars_and_duplicates():
    # 2026-09-09 had 09:08–09:14 pre-open bars in FYERS data: 382 bars
    df = candles_to_frame("NSE:SBIN-EQ", session_candles(DAY, start=time(9, 8), bars=382))
    df = pd.concat([df, df.tail(3)])
    cleaned = clean(df)
    assert len(cleaned) == SESSION_BARS
    assert cleaned["ts"].iloc[0].time() == time(9, 15)
    assert cleaned["ts"].iloc[-1].time() == time(15, 29)


def test_check_ok():
    assert check(frame(), SESSION_BARS, daily_volume=SESSION_BARS * 10) == ("ok", [])


def test_check_warns_on_missing_bars_and_volume_mismatch():
    status, issues = check(frame().head(300), SESSION_BARS, daily_volume=SESSION_BARS * 10)
    assert status == "warn"
    assert issues == ["300/375 bars", "1-min volume is 0.800x daily"]


def test_check_fails_on_bad_prices():
    df = frame()
    df.loc[5, "close"] = 0
    assert check(df, SESSION_BARS, None)[0] == "fail"
    df = frame()
    df.loc[5, "high"] = df.loc[5, "open"] - 5  # close 5% above high
    assert check(df, SESSION_BARS, None) == ("fail", ["high/low inconsistent with open/close (max 5.00%)"])


def test_check_warns_on_small_ohlc_gap():
    df = frame()
    df.loc[5, "high"] = df.loc[5, "close"] - 0.1  # close 0.1% above high, as in FYERS Nov 2023 data
    assert check(df, SESSION_BARS, None) == ("warn", ["1 bars with open/close outside high/low (max 0.10%)"])


# --- store ---

@pytest.fixture
def store(tmp_path):
    s = CandleStore(tmp_path)
    yield s
    s.close()


def test_chunk_roundtrip_and_completeness(store):
    df = frame()
    store.write_chunk("NSE:SBIN-EQ", "1m", "2026Q3", df, complete=False)
    assert not store.is_complete("NSE:SBIN-EQ", "1m", "2026Q3")
    store.write_chunk("NSE:SBIN-EQ", "1m", "2026Q3", df, complete=True)
    assert store.is_complete("NSE:SBIN-EQ", "1m", "2026Q3")
    assert not list(store.root.rglob("*.tmp"))
    pd.testing.assert_frame_equal(store.read_symbol("NSE:SBIN-EQ", DAY, DAY), df)
    assert store.read_symbol("NSE:SBIN-EQ", date(2026, 9, 10), date(2026, 9, 30)).empty


def test_build_month_writes_day_file_with_quality(store):
    index = "NSE:NIFTY50-INDEX"
    for symbol, df in [
        (index, frame(index, volume=0)),
        ("NSE:SBIN-EQ", frame("NSE:SBIN-EQ")),
        ("NSE:INFY-EQ", frame("NSE:INFY-EQ").head(370)),
    ]:
        store.write_chunk(symbol, "1m", "2026Q3", df, complete=True)
    for symbol in ("NSE:SBIN-EQ", "NSE:INFY-EQ"):
        daily = candles_to_frame(symbol, [[int(datetime.combine(DAY, time(), IST).timestamp()), 100, 101, 99, 100, 3750]])
        store.write_chunk(symbol, "1d", "2026", daily, complete=True)

    assert build_month(store, [index, "NSE:SBIN-EQ", "NSE:INFY-EQ"], date(2026, 9, 1), date(2026, 9, 30)) == 1
    assert store.days() == [DAY]
    day_df = store.read_day(DAY)
    assert len(day_df) == 375 * 2 + 370
    assert list(day_df["symbol"].unique()) == sorted(day_df["symbol"].unique())  # sorted by symbol
    quality = store.quality().set_index("symbol")
    assert quality.loc["NSE:SBIN-EQ", "status"] == "ok"
    assert quality.loc["NSE:INFY-EQ", "issues"] == "370/375 bars"  # volume 0.987x daily is within range
    assert store.read_day(DAY, symbols=["NSE:SBIN-EQ"])["symbol"].unique().tolist() == ["NSE:SBIN-EQ"]


# --- rate limiter ---

def test_rate_limiter_enforces_both_windows():
    now = [0.0]
    limiter = RateLimiter(per_second=3, per_minute=5, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    stamps = []
    for _ in range(7):
        limiter.wait()
        stamps.append(now[0])
    assert stamps[:3] == [0.0, 0.0, 0.0]
    assert stamps[3] == pytest.approx(1.0)       # 4th waits for the 1 s window
    assert stamps[5] == pytest.approx(60.0)      # 6th waits for the 60 s window
