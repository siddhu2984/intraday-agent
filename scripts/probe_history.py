"""Probe the limits of the FYERS history API before building the candle downloader.

Usage:  python scripts/probe_history.py
Read-only — places no orders. Calls are paced well under the FYERS rate limit.

Answers:
  1. How far back 1-min (and daily) history goes.
  2. The largest date range a single 1-min request accepts.
  3. Whether prices are adjusted for splits/bonuses (overnight jumps around known events).
  4. Whether volume is adjusted too (volume/turnover level shift across the ex-date).
  5. Typical latency per history call (to estimate full-download time).
"""

import math
import time
from datetime import date, datetime, timedelta

from agent.broker.fyers_auth import IST, client

DEPTH_SYMBOLS = ["NSE:SBIN-EQ", "NSE:RELIANCE-EQ"]
DEPTH_MONTHS = [1, 3, 6, 9, 12, 18, 24, 36, 60]
RANGE_DAYS = [30, 60, 90, 100, 101, 120, 180, 366]
PACE_S = 0.5  # ≈2 requests/second

# Corporate actions to test adjustment against: (symbol, ex-date, action, expected price factor).
# From memory — the probe prints what the data actually shows around each date.
EVENTS = [
    ("NSE:RELIANCE-EQ", date(2024, 10, 28), "1:1 bonus", 2.0),
    ("NSE:BAJFINANCE-EQ", date(2025, 6, 16), "4:1 bonus + 1:2 split", 10.0),
    ("NSE:NESTLEIND-EQ", date(2025, 8, 8), "1:1 bonus", 2.0),
    ("NSE:HDFCBANK-EQ", date(2025, 8, 26), "1:1 bonus", 2.0),
]
JUMP_THRESHOLD = 1.3  # overnight prev-close/open ratio beyond this is not normal trading

fyers = client()
latencies: list[float] = []


def history(symbol: str, resolution: str, start: date, end: date) -> dict:
    time.sleep(PACE_S)
    t0 = time.perf_counter()
    response = fyers.history({
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": start.isoformat(),
        "range_to": end.isoformat(),
        "cont_flag": "1",
    })
    latencies.append(time.perf_counter() - t0)
    return response


def day(ts: int) -> date:
    return datetime.fromtimestamp(ts, IST).date()


def describe(response: dict) -> str:
    if response.get("s") != "ok":
        return f"ERROR {response.get('code')}: {response.get('message')}"
    candles = response.get("candles", [])
    if not candles:
        return "ok, 0 candles"
    days = {day(c[0]) for c in candles}
    return f"ok, {len(candles):>6} candles, {len(days):>3} days, {min(days)} → {max(days)}"


def section(title: str) -> None:
    print(f"\n=== {title}")


def probe_depth(today: date) -> None:
    section("1. History depth — one week of data N months ago")
    for resolution, label in (("1", "1-min"), ("D", "daily")):
        for symbol in DEPTH_SYMBOLS:
            print(f"{label} {symbol}")
            for months in DEPTH_MONTHS:
                end = today - timedelta(days=round(months * 30.4))
                print(f"   {months:>2} months ago ({end}): {describe(history(symbol, resolution, end - timedelta(days=7), end))}")


def probe_range_limit(today: date) -> None:
    section("2. Largest date range per 1-min request (ending today)")
    for days in RANGE_DAYS:
        response = history(DEPTH_SYMBOLS[0], "1", today - timedelta(days=days), today)
        print(f"   {days:>3} days: {describe(response)}")


def overnight_jumps(candles: list) -> list[tuple[date, float]]:
    """(date, prev_close / open) for each day boundary where the ratio looks like a corporate action."""
    jumps = []
    for prev, cur in zip(candles, candles[1:]):
        if day(prev[0]) != day(cur[0]) and cur[1] > 0:
            ratio = prev[4] / cur[1]
            if ratio > JUMP_THRESHOLD or ratio < 1 / JUMP_THRESHOLD:
                jumps.append((day(cur[0]), ratio))
    return jumps


def probe_adjustment() -> None:
    section("3. Adjusted or raw? Overnight price ratio around known corporate actions")
    for symbol, ex_date, action, factor in EVENTS:
        print(f"{symbol}  {action}, ex-date {ex_date}, raw data would jump ≈{factor:g}×")
        window = (ex_date - timedelta(days=10), ex_date + timedelta(days=10))
        for resolution, label in (("D", "daily"), ("1", "1-min")):
            response = history(symbol, resolution, *window)
            candles = response.get("candles", [])
            if response.get("s") != "ok" or not candles:
                print(f"   {label:<5}: no data ({describe(response)})")
                continue
            before = [c for c in candles if day(c[0]) < ex_date]
            after = [c for c in candles if day(c[0]) >= ex_date]
            if not before or not after:
                print(f"   {label:<5}: data doesn't span the ex-date ({describe(response)})")
                continue
            ratio = before[-1][4] / after[0][1]
            verdict = "RAW (unadjusted)" if ratio > JUMP_THRESHOLD else "ADJUSTED (no jump)"
            print(f"   {label:<5}: close {day(before[-1][0])} = {before[-1][4]:.2f}, "
                  f"open {day(after[0][0])} = {after[0][1]:.2f}, ratio {ratio:.2f} → {verdict}")
            for jump_day, jump_ratio in overnight_jumps(candles):
                if jump_day != day(after[0][0]):
                    print(f"          other jump on {jump_day}: ratio {jump_ratio:.2f}")


def median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def probe_volume_adjustment() -> None:
    """Median daily volume/turnover in the 20 sessions after vs before each ex-date.

    Adjusted volume → ratio ≈ 1. Unadjusted volume → volume ratio ≈ factor, and turnover (price × volume)
    before the event looks `factor`× too small. SBIN over the same dates is the no-event control.
    """
    section("4. Volume adjusted? Median over 20 sessions after ÷ before the ex-date (daily bars)")
    print(f"   {'symbol':<18} {'factor':>6} {'volume':>7} {'turnover':>9}  1-min sum ÷ daily vol (day before)  verdict")
    for symbol, ex_date, _, factor in EVENTS:
        for sym, f in ((symbol, factor), ("NSE:SBIN-EQ", 1.0)):
            candles = history(sym, "D", ex_date - timedelta(days=45), ex_date + timedelta(days=45)).get("candles", [])
            before = [c for c in candles if day(c[0]) < ex_date][-20:]
            after = [c for c in candles if day(c[0]) >= ex_date][:20]
            if len(before) < 10 or len(after) < 10:
                print(f"   {sym:<18} not enough data around {ex_date}")
                continue
            vol_ratio = median([c[5] for c in after]) / median([c[5] for c in before])
            turn_ratio = median([c[4] * c[5] for c in after]) / median([c[4] * c[5] for c in before])

            last_day = day(before[-1][0])
            minute = history(sym, "1", last_day, last_day).get("candles", [])
            intraday_ratio = sum(c[5] for c in minute) / before[-1][5] if minute else float("nan")

            if f == 1.0:
                verdict = "control (no event)"
            else:
                # closer to 1 (adjusted) or to the factor (raw), on a log scale
                adjusted = abs(math.log(vol_ratio)) < abs(math.log(vol_ratio / f))
                verdict = "ADJUSTED" if adjusted else "RAW (unadjusted)"
            label = f"{f:g}×" if f != 1.0 else "—"
            print(f"   {sym:<18} {label:>6} {vol_ratio:>6.2f}× {turn_ratio:>8.2f}×  {intraday_ratio:>10.3f}"
                  f"{'':<23}  {verdict}   [{ex_date}]")


def main() -> None:
    today = datetime.now(IST).date()
    start = time.perf_counter()
    probe_depth(today)
    probe_range_limit(today)
    probe_adjustment()
    probe_volume_adjustment()

    section("5. Latency")
    ordered = sorted(latencies)
    print(f"   {len(latencies)} history calls, median {ordered[len(ordered) // 2]:.2f}s, "
          f"max {ordered[-1]:.2f}s (plus {PACE_S}s pacing each); total run {time.perf_counter() - start:.0f}s")


if __name__ == "__main__":
    main()
