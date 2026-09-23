"""Demo: how a candle file is built inside, and why the store is fast. Uses the pilot data in data/candles/.

Usage:  python scripts/demo_storage.py
Writes CSV and SQLite copies of the pilot data to a temp folder for comparison, then deletes them.
"""

import shutil
import sqlite3
import tempfile
import time
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from agent.data.store import CandleStore

SYMBOL = "NSE:SBIN-EQ"
store = CandleStore()
days = store.days()
DAY = days[-1]


def mb(n_bytes: float) -> str:
    return f"{n_bytes / 1e6:,.1f} MB" if n_bytes >= 1e6 else f"{n_bytes / 1e3:,.1f} KB"


def timed(fn, repeat: int = 3):
    """Best of `repeat` runs (files are in the OS cache after the first, as they would be in practice)."""
    best, result = float("inf"), None
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn()
        best = min(best, time.perf_counter() - t0)
    return best, result


def section(title: str) -> None:
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")


# ----------------------------------------------------------------------------------------------------------
section(f"1. WHAT IS IN A DAY FILE — {store.day_path(DAY).relative_to(store.root.parent.parent)}")
path = store.day_path(DAY)
df = pd.read_parquet(path)
print(f"A table: {len(df):,} rows (one per stock per minute) × {len(df.columns)} columns, sorted by symbol then time\n")
print(df.head(3).to_string())
print("   ...")
print(df.iloc[374:377].to_string(header=False))
print("   ...")

# ----------------------------------------------------------------------------------------------------------
section("2. HOW PARQUET LAYS IT OUT ON DISK — row groups → column chunks")
meta = pq.ParquetFile(path).metadata
print(f"File: {mb(path.stat().st_size)}, {meta.num_rows:,} rows, {meta.num_row_groups} row group(s), "
      f"{meta.num_columns} columns\n")
print("CSV/SQLite store row by row:   SBIN,09:15,590.1,590.9,...  SBIN,09:16,...  (all fields of a row together)")
print("Parquet stores column by column inside each row group:\n")
print("   [row group 0]  symbol: SBIN SBIN SBIN ... | ts: 09:15 09:16 ... | open: 590.1 590.4 ... | ... | volume ...")
print("   [row group 1]  ...\n")
print("Each column chunk carries min/max statistics and is compressed separately. This file's first row group:\n")
row_group = meta.row_group(0)
print(f"   {'column':<8} {'raw size':>10} {'on disk':>10} {'ratio':>6}  {'encoding':<34} min → max")
for i in range(row_group.num_columns):
    col = row_group.column(i)
    stats = col.statistics
    lo, hi = (stats.min, stats.max) if stats is not None and stats.has_min_max else ("", "")
    encodings = ",".join(e for e in col.encodings if e != "RLE")
    print(f"   {col.path_in_schema:<8} {mb(col.total_uncompressed_size):>10} {mb(col.total_compressed_size):>10} "
          f"{col.total_uncompressed_size / col.total_compressed_size:>5.1f}x  {encodings:<34} {lo} → {hi}")
print("\nHow each column shrinks (two steps):")
print("   1. Dictionary encoding — each distinct value is stored once, rows hold small integer codes:")
print(f"        symbol: {df['symbol'].nunique()} distinct values for {len(df):,} rows")
print(f"        ts:     {df['ts'].nunique()} distinct minutes for {len(df):,} rows")
print(f"        close:  {df['close'].nunique():,} distinct prices for {len(df):,} rows (prices move in 5-paise ticks)")
print("   2. zstd compression over those codes — repeated runs (same stock, neighbouring minutes) shrink further.")
print("   (ts statistics print in UTC: Parquet stores instants in UTC; they read back as IST.)")

print(f"\nRow groups and their symbol ranges (min/max statistics):")
for i in range(meta.num_row_groups):
    stats = meta.row_group(i).column(0).statistics
    print(f"   row group {i}: {meta.row_group(i).num_rows:>6,} rows, symbol {stats.min} … {stats.max}")
print("A reader asking for one symbol checks these ranges and skips row groups that cannot contain it.\n"
      "(With 21 pilot stocks a day fits in one row group; with 500 stocks there are ~8 per day file.)")

# ----------------------------------------------------------------------------------------------------------
section("3. COLUMNAR READS — read only the columns you need")
t_all, _ = timed(lambda: pd.read_parquet(path))
t_close, _ = timed(lambda: pd.read_parquet(path, columns=["symbol", "ts", "close"]))
print(f"   all 7 columns             {t_all * 1000:6.1f} ms")
print(f"   symbol, ts, close only    {t_close * 1000:6.1f} ms   (the other columns' bytes are never read)")
print("   A CSV reader must scan every character of every row, even to get one column.")

# ----------------------------------------------------------------------------------------------------------
section("4. SAME DATA, THREE FORMATS — build CSV and SQLite copies of the whole pilot store")
tmp = Path(tempfile.mkdtemp(prefix="candle_demo_"))
try:
    everything = pd.concat([store.read_day(d, include_failed=True) for d in days], ignore_index=True)
    print(f"Pilot data: {len(everything):,} candles, {everything['symbol'].nunique()} symbols, {len(days)} days\n")

    csv_dir = tmp / "csv"
    csv_dir.mkdir()
    t0 = time.perf_counter()
    for d, frame in everything.groupby(everything["ts"].dt.date):
        frame.to_csv(csv_dir / f"{d}.csv", index=False)
    csv_write = time.perf_counter() - t0

    db_path = tmp / "candles.sqlite"
    t0 = time.perf_counter()
    with sqlite3.connect(db_path) as db:
        flat = everything.assign(day=everything["ts"].dt.date.astype(str), ts=everything["ts"].astype(str))
        flat.to_sql("candles", db, index=False, chunksize=100_000)
        db.execute("CREATE INDEX by_day ON candles (day)")
        db.execute("CREATE INDEX by_symbol ON candles (symbol)")
    sqlite_write = time.perf_counter() - t0

    parquet_size = sum(p.stat().st_size for p in (store.root / "1m").glob("*.parquet"))
    by_symbol_size = sum(p.stat().st_size for p in store.symbol_dir(SYMBOL).glob("1m_*.parquet"))
    csv_size = sum(p.stat().st_size for p in csv_dir.glob("*.csv"))
    print(f"   {'format':<32} {'size on disk':>13}")
    print(f"   {'CSV, one file per day':<32} {mb(csv_size):>13}")
    print(f"   {'SQLite, one table + 2 indexes':<32} {mb(db_path.stat().st_size):>13}")
    print(f"   {'Parquet (ours), one file per day':<32} {mb(parquet_size):>13}")
    print(f"   (building the copies took {csv_write:.0f} s for CSV, {sqlite_write:.0f} s for SQLite)")

    # --------------------------------------------------------------------------------------------------
    section("5. SPEED — the three questions the agent actually asks")
    db = sqlite3.connect(db_path)
    day_s = str(DAY)
    last_20 = days[-20:]

    questions = [
        (f"A. All stocks, one day ({DAY}) — backtest replay step", [
            ("CSV", lambda: pd.read_csv(csv_dir / f"{day_s}.csv")),
            ("SQLite", lambda: pd.read_sql("SELECT * FROM candles WHERE day = ?", db, params=(day_s,))),
            ("Parquet", lambda: pd.read_parquet(store.day_path(DAY))),
        ]),
        ("B. All stocks, last 20 days — morning load", [
            ("CSV", lambda: pd.concat(pd.read_csv(csv_dir / f"{d}.csv") for d in last_20)),
            ("SQLite", lambda: pd.read_sql("SELECT * FROM candles WHERE day >= ?", db, params=(str(last_20[0]),))),
            ("Parquet", lambda: pd.concat(pd.read_parquet(store.day_path(d)) for d in last_20)),
        ]),
        (f"C. One stock, 3 years ({SYMBOL}) — research", [
            ("CSV (must open all 742 files)", lambda: pd.concat(
                f[f["symbol"] == SYMBOL] for f in (pd.read_csv(p) for p in sorted(csv_dir.glob("*.csv"))))),
            ("SQLite (symbol index)", lambda: pd.read_sql("SELECT * FROM candles WHERE symbol = ?", db, params=(SYMBOL,))),
            ("Parquet by-day (742 files, filtered)", lambda: pd.concat(
                pd.read_parquet(store.day_path(d), filters=[("symbol", "==", SYMBOL)]) for d in days)),
            ("Parquet by_symbol (13 files)", lambda: store.read_symbol(SYMBOL, days[0], days[-1])),
        ]),
    ]
    for title, contenders in questions:
        print(f"\n{title}")
        results = []
        for name, fn in contenders:
            seconds, result = timed(fn, repeat=1 if name.startswith("CSV (must") else 3)
            results.append((name, seconds, len(result)))
        fastest = min(s for _, s, _ in results)
        for name, seconds, rows in results:
            bar = "█" * max(1, round(40 * seconds / max(s for _, s, _ in results)))
            print(f"   {name:<38} {seconds * 1000:>9,.0f} ms  {seconds / fastest:>6.1f}x  {bar}  ({rows:,} rows)")
    db.close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

section("6. SUMMARY")
print("""   Parquet is small and fast here because of three things:
   1. Columnar + compressed — similar values sit together and shrink: ~5x smaller than CSV, ~9x smaller
      than SQLite here. Less disk means less to read.
   2. Typed binary — numbers and timestamps are stored as numbers, not text; no parsing on read.
   3. Statistics — min/max per row group let readers skip data that can't match (the symbol filter).
   And the layout matches the questions: by-day for A and B, by_symbol for C.
   SQLite's index helps it on C, but it is the slowest on bulk reads (A, B) — and the backtest does A 742 times.
   CSV is fine for one day but hopeless for C: it has no way to skip rows, so it parses every file.""")
store.close()
