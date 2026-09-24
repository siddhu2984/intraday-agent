"""Per-day, per-stock screening inputs for the backtest (architecture.md §4.4), all knowable by 09:30.

Two layers:
  opening.parquet   (data/features/) raw first-15-min stats per (day, symbol) from the 1-min day files — slow to
                    extract, so cached and extended incrementally.
  build_features()  everything else, computed in memory from daily bars + opening stats + NSE reference data:

    prev_close, open, gap_pct        open = the pre-open auction price (see §4.2b)
    turnover_cr                      average close × volume over the previous `lookback_days` sessions, ₹ crore
    atr_pct                          Wilder ATR(14) of daily bars up to yesterday, % of prev_close
    or_high, or_low, or_close        09:15–09:29 high/low and the 09:29 bar's close
    or_volume, or_volume_avg         09:15–09:29 volume, and its average over the previous `lookback_days` sessions
    series, band_pct                 NSE daily security list (band NaN = 'No Band', F&O)
    results, ex_date                 NSE corporate events that day
    member, sector                   point-in-time Nifty 500 membership and NSE industry
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pandas as pd

from agent.broker.fyers_auth import PROJECT_ROOT
from agent.data.download import INDEX_SYMBOL
from agent.data.indicators import wilder_atr
from agent.data.store import CandleStore

FEATURES_DIR = PROJECT_ROOT / "data" / "features"
REFERENCE_DIR = PROJECT_ROOT / "data" / "reference"
MEMBERS_PATH = PROJECT_ROOT / "config" / "universe" / "nifty500_members.csv"
OPENING_COLUMNS = ["day", "symbol", "or_high", "or_low", "or_close", "or_volume", "or_bars"]
OPENING_RANGE_END = time(9, 30)
MIN_HISTORY = 0.75  # rolling averages need 75% of lookback_days (15 of 20)


def extract_opening(store: CandleStore, day: date) -> pd.DataFrame:
    bars = store.read_day(day)
    bars = bars[bars["ts"].dt.time < OPENING_RANGE_END]
    g = bars.groupby("symbol", sort=True)
    out = pd.DataFrame({"or_high": g["high"].max(), "or_low": g["low"].min(), "or_close": g["close"].last(),
                        "or_volume": g["volume"].sum(), "or_bars": g.size()}).reset_index()
    out.insert(0, "day", day)
    return out[OPENING_COLUMNS]


def update_opening(store: CandleStore, days: list[date], path=FEATURES_DIR / "opening.parquet") -> pd.DataFrame:
    """Opening stats for `days`, extracting only days not already cached."""
    cached = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=OPENING_COLUMNS)
    have = set(cached["day"])
    missing = [d for d in days if d not in have and store.day_path(d).exists()]
    if missing:
        print(f"extracting opening stats for {len(missing)} days", flush=True)
        cached = pd.concat([cached, *(extract_opening(store, d) for d in missing)], ignore_index=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        cached.sort_values(["day", "symbol"]).to_parquet(path, index=False)
    return cached[cached["day"].isin(set(days))]


def read_members(path=MEMBERS_PATH) -> pd.DataFrame:
    m = pd.read_csv(path, dtype=str, keep_default_na=False)
    m = m[m["fyers_symbol"] != ""].copy()
    m["start"] = pd.to_datetime(m["start"]).dt.date
    m["end"] = pd.to_datetime(m["end"].replace("", None)).dt.date
    return m


def _daily_bars(store: CandleStore, symbols: list[str], start: date, end: date) -> pd.DataFrame:
    frames = [store.read_symbol(s, start, end, "1d") for s in symbols]
    df = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    df["day"] = df["ts"].dt.date
    return df.drop(columns="ts").sort_values(["symbol", "day"], ignore_index=True)


def _per_symbol_rolling_mean(df: pd.DataFrame, column: str, lookback: int) -> pd.Series:
    """Mean of the previous `lookback` rows of the same symbol (today excluded)."""
    return df.groupby("symbol")[column].transform(
        lambda s: s.shift(1).rolling(lookback, min_periods=int(lookback * MIN_HISTORY)).mean())


def build_features(start: date, end: date, lookback_days: int, store: CandleStore | None = None,
                   trading_days: list[date] | None = None, reference_dir=REFERENCE_DIR) -> pd.DataFrame:
    """One row per (day, symbol) for start <= day <= end: members plus the index."""
    own_store = store is None
    store = store or CandleStore()
    try:
        members = read_members()
        symbols = sorted(set(members["fyers_symbol"])) + [INDEX_SYMBOL]
        history_start = start - timedelta(days=lookback_days * 3)  # enough calendar days for the rolling windows
        daily = _daily_bars(store, symbols, history_start, end)

        daily["prev_close"] = daily.groupby("symbol")["close"].shift(1)
        daily["gap_pct"] = (daily["open"] / daily["prev_close"] - 1) * 100
        daily["turnover"] = daily["close"] * daily["volume"] / 1e7
        daily["turnover_cr"] = _per_symbol_rolling_mean(daily, "turnover", lookback_days)
        atr = daily.groupby("symbol", group_keys=False)[["high", "low", "close"]].apply(wilder_atr)
        daily["atr_pct"] = atr.groupby(daily["symbol"]).shift(1) / daily["prev_close"] * 100

        days = trading_days or sorted(set(daily["day"]))
        opening = update_opening(store, [d for d in days if history_start <= d <= end])
        opening = opening.sort_values(["symbol", "day"], ignore_index=True)
        opening["or_volume_avg"] = _per_symbol_rolling_mean(opening, "or_volume", lookback_days)

        df = daily.merge(opening, on=["day", "symbol"], how="left")
        df = df[(df["day"] >= start) & (df["day"] <= end)]
    finally:
        if own_store:
            store.close()

    status = pd.read_parquet(reference_dir / "daily_status.parquet").rename(columns={"fyers_symbol": "symbol"})
    df = df.merge(status[["day", "symbol", "series", "band_pct"]], on=["day", "symbol"], how="left")
    events = pd.read_parquet(reference_dir / "corporate_events.parquet").rename(columns={"fyers_symbol": "symbol"})
    for kind in ("results", "ex_date"):
        keys = set(zip(events.loc[events["kind"] == kind, "day"], events.loc[events["kind"] == kind, "symbol"]))
        df[kind] = [k in keys for k in zip(df["day"], df["symbol"])]

    df["member"] = False
    df["sector"] = None
    for m in members.itertuples():
        rows = (df["symbol"] == m.fyers_symbol) & (df["day"] >= m.start)
        if m.end is not None and not pd.isna(m.end):
            rows &= df["day"] < m.end
        df.loc[rows, "member"] = True
        df.loc[rows, "sector"] = m.industry or None
    df.loc[df["symbol"] == INDEX_SYMBOL, "sector"] = None
    return df.drop(columns=["turnover"]).sort_values(["day", "symbol"], ignore_index=True)


def features_path(start: date, end: date, lookback_days: int):
    return FEATURES_DIR / f"features_{start:%Y%m%d}_{end:%Y%m%d}_L{lookback_days}.parquet"


def load_features(start: date, end: date, lookback_days: int, rebuild: bool = False) -> pd.DataFrame:
    """build_features, cached per (start, end, lookback)."""
    path = features_path(start, end, lookback_days)
    if path.exists() and not rebuild:
        return pd.read_parquet(path)
    df = build_features(start, end, lookback_days)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return df

