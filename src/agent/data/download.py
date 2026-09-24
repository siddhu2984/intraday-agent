"""Download FYERS history into the candle store (architecture.md §4.2b).

Two stages:
  fetch  per symbol: 1-min candles in calendar quarters (FYERS allows <= 100 days per 1-min request) and daily
         bars in calendar years, into by_symbol/. Resumable: chunks recorded as complete are skipped; the chunk
         holding the latest session is re-fetched on every run.
  build  per month: regroup the 1-min candles into one file per trading day, with quality checks.

Usage:
  python -m agent.data.download --universe config/universe/pilot.csv --years 3
  python -m agent.data.download --universe config/universe/pilot.csv --stage build
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from agent.broker.fyers_auth import IST
from agent.broker.ratelimit import RateLimiter
from agent.data.quality import SESSION_BARS, check, clean
from agent.data.store import COLUMNS, CandleStore, candles_to_frame, chunk_range

INDEX_SYMBOL = "NSE:NIFTY50-INDEX"
SESSION_FINAL = (15, 45)  # after this IST time, today's candles are final
FYERS_RESOLUTION = {"1m": "1", "1d": "D"}
RETRIES = 3
PER_SECOND, PER_MINUTE = 3, 150  # below FYERS' published 10/s and 200/min


class DownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class Chunk:
    name: str        # '2025Q3' or '2025'
    start: date
    end: date        # clipped to the last final session
    complete: bool   # the whole calendar period is in the past


def chunks(resolution: str, start: date, end: date) -> list[Chunk]:
    """Calendar quarters (1m) or years (1d) overlapping [start, end]."""
    names = []
    for year in range(start.year, end.year + 1):
        names += [f"{year}Q{q}" for q in range(1, 5)] if resolution == "1m" else [str(year)]
    result = []
    for name in names:
        chunk_start, chunk_end = chunk_range(name)
        if chunk_start <= end and chunk_end >= start:
            result.append(Chunk(name, chunk_start, min(chunk_end, end), complete=chunk_end <= end))
    return result


def last_final_session(now: datetime) -> date:
    return now.date() if (now.hour, now.minute) >= SESSION_FINAL else now.date() - timedelta(days=1)


def read_universe(path: Path) -> list[str]:
    """FYERS symbols from a CSV's 'fyers_symbol' column (e.g. nifty500_members.csv) or 'symbol' column (pilot.csv).

    Rows without a FYERS symbol (delisted stocks FYERS has no history for) are skipped; the index is always added.
    """
    df = pd.read_csv(path, dtype=str)
    column = "fyers_symbol" if "fyers_symbol" in df.columns else "symbol"
    symbols = list(dict.fromkeys(df[column].dropna().str.strip()))
    return symbols if INDEX_SYMBOL in symbols else symbols + [INDEX_SYMBOL]


def fetch_history(fyers, limiter: RateLimiter, symbol: str, resolution: str, chunk: Chunk) -> list:
    request = {
        "symbol": symbol,
        "resolution": FYERS_RESOLUTION[resolution],
        "date_format": "1",
        "range_from": chunk.start.isoformat(),
        "range_to": chunk.end.isoformat(),
        "cont_flag": "1",
    }
    error = ""
    for attempt in range(1, RETRIES + 1):
        limiter.wait()
        try:
            response = fyers.history(request)
        except Exception as exc:  # network errors surface as exceptions from the SDK
            error = repr(exc)
        else:
            if response.get("s") == "ok":
                return response.get("candles", [])
            if response.get("s") == "no_data":
                return []
            error = f"{response.get('code')}: {response.get('message')}"
        if attempt < RETRIES:
            time.sleep(2 ** attempt)
    raise DownloadError(f"{symbol} {resolution} {chunk.name}: {error}")


def fetch(store: CandleStore, fyers, symbols: list[str], start: date, end: date) -> list[str]:
    """Download missing chunks. Returns the error for each chunk that failed after retries."""
    todo = [
        (symbol, resolution, chunk)
        for symbol in symbols
        for resolution in ("1m", "1d")
        for chunk in chunks(resolution, start, end)
        if not store.is_complete(symbol, resolution, chunk.name)
    ]
    print(f"fetch: {len(todo)} chunks to download for {len(symbols)} symbols, {start} → {end} "
          f"(≈{len(todo) / PER_MINUTE:.0f} min at {PER_MINUTE}/min)")
    limiter = RateLimiter(PER_SECOND, PER_MINUTE)
    errors = []
    for i, (symbol, resolution, chunk) in enumerate(todo, 1):
        try:
            df = candles_to_frame(symbol, fetch_history(fyers, limiter, symbol, resolution, chunk))
        except DownloadError as exc:
            errors.append(str(exc))
            print(f"   FAILED {exc}")
            continue
        if resolution == "1m":
            df = clean(df)
        store.write_chunk(symbol, resolution, chunk.name, df, chunk.complete)
        if i % 25 == 0 or i == len(todo):
            print(f"   {i}/{len(todo)}  {symbol} {resolution} {chunk.name}: {len(df)} candles")
    return errors


def months(start: date, end: date) -> list[tuple[date, date]]:
    result, first = [], start.replace(day=1)
    while first <= end:
        next_first = (first + timedelta(days=32)).replace(day=1)
        result.append((max(first, start), min(next_first - timedelta(days=1), end)))
        first = next_first
    return result


def _concat(frames) -> pd.DataFrame:
    """Concatenate, skipping empty frames (they would turn the typed columns into object dtype)."""
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)


def build_month(store: CandleStore, symbols: list[str], start: date, end: date) -> int:
    """Rebuild the day files for [start, end]. Returns the number of days written."""
    minute = _concat(store.read_symbol(s, start, end, "1m") for s in symbols)
    if minute.empty:
        return 0
    daily = _concat(store.read_symbol(s, start, end, "1d") for s in symbols)
    daily_volume = {(row.symbol, row.ts.date()): row.volume for row in daily.itertuples()}

    minute["day"] = minute["ts"].dt.date
    span = minute.groupby("symbol")["day"].agg(["min", "max"])  # a stock's listed days within this month

    for day, bars in minute.groupby("day"):
        index_bars = int((bars["symbol"] == INDEX_SYMBOL).sum())
        expected = index_bars or SESSION_BARS  # short special sessions (e.g. Muhurat) are short for the index too
        rows = []
        for symbol, symbol_bars in bars.groupby("symbol"):
            status, issues = check(
                symbol_bars, expected, None if symbol == INDEX_SYMBOL else daily_volume.get((symbol, day))
            )
            rows.append((symbol, len(symbol_bars), status, "; ".join(issues)))
        present = set(bars["symbol"])
        for symbol, (first, last) in span.iterrows():
            if symbol not in present and first < day < last:
                rows.append((symbol, 0, "warn", "no bars on a trading day"))
        store.write_day(day, bars.drop(columns="day"), rows)
    return minute["day"].nunique()


def build(store: CandleStore, symbols: list[str], start: date, end: date) -> None:
    total = 0
    for month_start, month_end in months(start, end):
        total += build_month(store, symbols, month_start, month_end)
    print(f"build: {total} day files written, {start} → {end}")

    quality = store.quality()
    quality = quality[(quality["day"] >= start.isoformat()) & (quality["day"] <= end.isoformat())]
    print("quality (symbol-days):", quality["status"].value_counts().to_dict())
    problems = quality[quality["status"] != "ok"]
    if not problems.empty:
        print("most common issues:")
        issue_kinds = problems["issues"].str.replace(r"[\d.]+", "N", regex=True)
        for issue, count in issue_kinds.value_counts().head(10).items():
            print(f"   {count:>6}  {issue}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download FYERS history into the candle store.")
    parser.add_argument("--universe", type=Path, required=True, help="CSV with a 'symbol' column")
    parser.add_argument("--years", type=float, default=3.0, help="history to cover, ending at the last final session")
    parser.add_argument("--stage", choices=["fetch", "build", "all"], default="all")
    args = parser.parse_args(argv)

    symbols = read_universe(args.universe)
    end = last_final_session(datetime.now(IST))
    start = end - timedelta(days=round(args.years * 365.25))
    store = CandleStore()
    errors: list[str] = []
    try:
        if args.stage in ("fetch", "all"):
            from agent.broker.fyers_auth import client  # only needed when talking to FYERS

            errors = fetch(store, client(), symbols, start, end)
        if args.stage in ("build", "all"):
            build(store, symbols, start, end)
    finally:
        store.close()

    if errors:
        print(f"\n{len(errors)} chunks failed — re-run to retry them:")
        for error in errors:
            print(f"   {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
