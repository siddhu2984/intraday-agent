"""Historical candle store (architecture.md §4.2b).

The same 1-min candles in two layouts, plus daily bars:

  by_symbol/<SYMBOL>/1m_<YYYY>Q<n>.parquet   one stock, one calendar quarter — "one stock over years" reads few files
  by_symbol/<SYMBOL>/1d_<YYYY>.parquet       one stock, one calendar year of daily bars
  1m/<YYYY-MM-DD>.parquet                    every stock for one day, sorted by symbol — backtest replay, morning load
  manifest.sqlite                            downloaded chunks + per-day quality results
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from agent.broker.fyers_auth import IST, PROJECT_ROOT

DEFAULT_ROOT = PROJECT_ROOT / "data" / "candles"
COLUMNS = ["symbol", "ts", "open", "high", "low", "close", "volume"]
ROW_GROUP_ROWS = 25_000  # ~65 stocks per row group, so a one-symbol read skips most of a day file

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    symbol TEXT, resolution TEXT, chunk TEXT,
    candles INTEGER, complete INTEGER, fetched_at TEXT,
    PRIMARY KEY (symbol, resolution, chunk)
);
CREATE TABLE IF NOT EXISTS quality (
    day TEXT, symbol TEXT, bars INTEGER, status TEXT, issues TEXT, built_at TEXT,
    PRIMARY KEY (day, symbol)
);
"""


def candles_to_frame(symbol: str, candles: list) -> pd.DataFrame:
    """FYERS [[epoch, open, high, low, close, volume], ...] → a frame with COLUMNS."""
    df = pd.DataFrame(candles, columns=["epoch", "open", "high", "low", "close", "volume"])
    df = df.astype({"open": "float64", "high": "float64", "low": "float64", "close": "float64", "volume": "int64"})
    df.insert(0, "symbol", symbol)
    ts = pd.to_datetime(df.pop("epoch"), unit="s", utc=True).dt.tz_convert(str(IST))
    df.insert(1, "ts", ts.astype(f"datetime64[ms, {IST}]"))  # ms: the unit Parquet round-trips
    return df


def chunk_range(chunk: str) -> tuple[date, date]:
    """'2025Q3' → (2025-07-01, 2025-09-30); '2025' → (2025-01-01, 2025-12-31)."""
    if "Q" in chunk:
        year, quarter = int(chunk[:4]), int(chunk[5])
        start = date(year, 3 * quarter - 2, 1)
        next_start = date(year + 1, 1, 1) if quarter == 4 else date(year, 3 * quarter + 1, 1)
        return start, next_start - timedelta(days=1)
    year = int(chunk)
    return date(year, 1, 1), date(year, 12, 31)


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Write via a temp file + rename, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False, compression="zstd", row_group_size=ROW_GROUP_ROWS)
    os.replace(tmp, path)


class CandleStore:
    def __init__(self, root: Path = DEFAULT_ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.root / "manifest.sqlite")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # --- by-symbol layout (what the downloader writes) ---

    def symbol_dir(self, symbol: str) -> Path:
        return self.root / "by_symbol" / symbol.replace(":", "_")  # ':' is not allowed in Windows paths

    def chunk_path(self, symbol: str, resolution: str, chunk: str) -> Path:
        return self.symbol_dir(symbol) / f"{resolution}_{chunk}.parquet"

    def is_complete(self, symbol: str, resolution: str, chunk: str) -> bool:
        row = self._db.execute(
            "SELECT complete FROM chunks WHERE symbol = ? AND resolution = ? AND chunk = ?",
            (symbol, resolution, chunk),
        ).fetchone()
        return bool(row and row[0]) and self.chunk_path(symbol, resolution, chunk).exists()

    def write_chunk(self, symbol: str, resolution: str, chunk: str, df: pd.DataFrame, complete: bool) -> None:
        _write_parquet(df, self.chunk_path(symbol, resolution, chunk))
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO chunks VALUES (?, ?, ?, ?, ?, ?)",
                (symbol, resolution, chunk, len(df), int(complete), datetime.now(IST).isoformat(timespec="seconds")),
            )

    def read_symbol(self, symbol: str, start: date, end: date, resolution: str = "1m") -> pd.DataFrame:
        """One stock's candles with start <= day <= end, read from the few chunk files that overlap."""
        frames = []
        for path in sorted(self.symbol_dir(symbol).glob(f"{resolution}_*.parquet")):
            chunk_start, chunk_end = chunk_range(path.stem.split("_", 1)[1])
            if chunk_start <= end and chunk_end >= start:
                frames.append(pd.read_parquet(path))
        if not frames:
            return pd.DataFrame(columns=COLUMNS)
        df = pd.concat(frames, ignore_index=True)
        days = df["ts"].dt.date
        return df[(days >= start) & (days <= end)].sort_values("ts", ignore_index=True)

    # --- by-day layout (built from by-symbol) ---

    def day_path(self, day: date) -> Path:
        return self.root / "1m" / f"{day.isoformat()}.parquet"

    def write_day(self, day: date, df: pd.DataFrame, quality_rows: list[tuple]) -> None:
        """Replace a day file and its quality rows: (symbol, bars, status, issues)."""
        _write_parquet(df.sort_values(["symbol", "ts"], ignore_index=True), self.day_path(day))
        built_at = datetime.now(IST).isoformat(timespec="seconds")
        with self._db:
            self._db.execute("DELETE FROM quality WHERE day = ?", (day.isoformat(),))
            self._db.executemany(
                "INSERT INTO quality VALUES (?, ?, ?, ?, ?, ?)",
                [(day.isoformat(), *row, built_at) for row in quality_rows],
            )

    def days(self) -> list[date]:
        return sorted(date.fromisoformat(p.stem) for p in (self.root / "1m").glob("*.parquet"))

    def read_day(self, day: date, symbols: list[str] | None = None, include_failed: bool = False) -> pd.DataFrame:
        filters = [("symbol", "in", symbols)] if symbols else None
        df = pd.read_parquet(self.day_path(day), filters=filters)
        if not include_failed:
            failed = {s for (s,) in self._db.execute(
                "SELECT symbol FROM quality WHERE day = ? AND status = 'fail'", (day.isoformat(),))}
            df = df[~df["symbol"].isin(failed)]
        return df.reset_index(drop=True)

    def quality(self) -> pd.DataFrame:
        return pd.read_sql("SELECT day, symbol, bars, status, issues FROM quality ORDER BY day, symbol", self._db)
