"""Build per-day NSE reference data for the backtest (architecture.md §4.4 Stage A, §4.6 hard blocks).

Usage:  python scripts/build_nse_reference.py [--start 2023-09-25] [--end 2026-09-23]

Outputs (data/reference/):
  daily_status.parquet      day, fyers_symbol, nse_symbol, series, band_pct (NaN = 'No Band', i.e. F&O)
                            for every Nifty 500 member-day. Series BE/BZ = trade-for-trade.
  corporate_events.parquet  day, fyers_symbol, kind ('results' | 'ex_date'), detail

Sources (cached in data/nse_archives/):
  sec_list_DDMMYYYY.csv     NSE's daily security list: symbol, series, price band
  bhavcopy                  for ISINs, so a day's NSE symbol maps to our stock across renames and splits
                            (UDiFF format from Jul 2024, the older cmDDMONYYYYbhav format before)
  corporate APIs            board meetings (financial results) and corporate actions (ex-dates)

A member is matched by ISIN, else by NSE symbol. Member-days with no match are listed (a stock not trading
that day is expected, e.g. RELINFRA's call-auction days).
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import re
import sys
import zipfile
from pathlib import Path

import pandas as pd

from agent.broker.fyers_auth import PROJECT_ROOT
from agent.data.nse_http import Fetcher
from agent.ops.calendar import NseCalendar

CACHE = PROJECT_ROOT / "data" / "nse_archives"
OUT = PROJECT_ROOT / "data" / "reference"
MEMBERS = PROJECT_ROOT / "config" / "universe" / "nifty500_members.csv"

SEC_LIST_URL = "https://nsearchives.nseindia.com/content/equities/sec_list_{:%d%m%Y}.csv"
UDIFF_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{:%Y%m%d}_F_0000.csv.zip"
OLD_BHAV_URL = "https://nsearchives.nseindia.com/content/historical/EQUITIES/{0:%Y}/{1}/cm{0:%d}{1}{0:%Y}bhav.csv.zip"
HOME_URL = "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings"
BOARD_URL = "https://www.nseindia.com/api/corporate-board-meetings?index=equities&from_date={:%d-%m-%Y}&to_date={:%d-%m-%Y}"
ACTIONS_URL = "https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date={:%d-%m-%Y}&to_date={:%d-%m-%Y}"
EQUITY_SERIES = ("EQ", "BE", "BZ")
NOT_PRICE_EVENTS = re.compile(r"general meeting|^agm$|^egm$", re.I)  # book closures with no price effect


def read_bhavcopy(fetcher: Fetcher, day: dt.date) -> pd.DataFrame | None:
    """symbol, series, isin for one day, from whichever bhavcopy format exists."""
    data = fetcher.get(UDIFF_URL.format(day), CACHE / "bhav" / f"udiff_{day:%Y%m%d}.zip")
    if data:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            df = pd.read_csv(z.open(z.namelist()[0]), dtype=str)
        df = df[df["FinInstrmTp"] == "STK"]
        return pd.DataFrame({"symbol": df["TckrSymb"], "series": df["SctySrs"], "isin": df["ISIN"]})
    month = day.strftime("%b").upper()
    data = fetcher.get(OLD_BHAV_URL.format(day, month), CACHE / "bhav" / f"old_{day:%Y%m%d}.zip")
    if data:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            df = pd.read_csv(z.open(z.namelist()[0]), dtype=str)
        df.columns = [c.strip() for c in df.columns]
        return pd.DataFrame({"symbol": df["SYMBOL"].str.strip(), "series": df["SERIES"].str.strip(),
                             "isin": df["ISIN"].str.strip()})
    return None


def read_sec_list(fetcher: Fetcher, day: dt.date) -> pd.DataFrame | None:
    data = fetcher.get(SEC_LIST_URL.format(day), CACHE / "sec_list" / f"{day:%Y%m%d}.csv")
    if not data:
        return None
    df = pd.read_csv(io.BytesIO(data), dtype=str)
    df.columns = [c.strip() for c in df.columns]
    band = pd.to_numeric(df["Band"].str.strip(), errors="coerce")  # 'No Band' → NaN
    return pd.DataFrame({"symbol": df["Symbol"].str.strip(), "series": df["Series"].str.strip(), "band_pct": band})


def members_on(members: pd.DataFrame, day: dt.date) -> pd.DataFrame:
    d = day.isoformat()
    return members[(members["start"] <= d) & ((members["end"] == "") | (members["end"] > d))]


def daily_status(fetcher: Fetcher, members: pd.DataFrame, days: list[dt.date]) -> tuple[pd.DataFrame, list]:
    rows, misses = [], []
    for i, day in enumerate(days, 1):
        bhav, sec = read_bhavcopy(fetcher, day), read_sec_list(fetcher, day)
        if bhav is None or sec is None:
            sys.exit(f"{day}: missing {'bhavcopy' if bhav is None else 'sec_list'} — is it a trading day?")
        bhav = bhav[bhav["series"].isin(EQUITY_SERIES)]
        by_isin = bhav.drop_duplicates("isin").set_index("isin")
        by_symbol = bhav.drop_duplicates("symbol").set_index("symbol")
        bands = sec.drop_duplicates(["symbol", "series"]).set_index(["symbol", "series"])["band_pct"]
        for m in members_on(members, day).itertuples():
            hit = by_isin.loc[m.isin] if m.isin in by_isin.index else (
                by_symbol.loc[m.symbol] if m.symbol in by_symbol.index else None)
            if hit is None:
                misses.append((day, m.fyers_symbol))
                continue
            nse_symbol = hit["symbol"] if "symbol" in hit.index else m.symbol
            band = bands.get((nse_symbol, hit["series"]), float("nan"))
            rows.append((day, m.fyers_symbol, nse_symbol, hit["series"], band))
        if i % 50 == 0:
            print(f"  {i}/{len(days)} days", flush=True)
    df = pd.DataFrame(rows, columns=["day", "fyers_symbol", "nse_symbol", "series", "band_pct"])
    return df, misses


def month_chunks(start: dt.date, end: dt.date):
    cur = start.replace(day=1)
    while cur <= end:
        nxt = (cur + dt.timedelta(days=32)).replace(day=1)
        yield max(cur, start), min(nxt - dt.timedelta(days=1), end)
        cur = nxt


def corporate_events(fetcher: Fetcher, members: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DataFrame:
    by_isin = dict(zip(members["isin"], members["fyers_symbol"]))
    by_symbol = dict(zip(members["symbol"], members["fyers_symbol"]))

    def ours(isin, symbol):
        return by_isin.get(isin) or by_symbol.get(symbol)

    rows = []
    for lo, hi in month_chunks(start, end):
        for r in fetcher.api(BOARD_URL.format(lo, hi), CACHE / "board" / f"{lo:%Y%m}.json"):
            purpose = r.get("bm_purpose") or ""
            fyers = ours(r.get("sm_isin"), r.get("bm_symbol"))
            if fyers and re.search(r"financial result", purpose, re.I):
                day = dt.datetime.strptime(r["bm_date"], "%d-%b-%Y").date()
                rows.append((day, fyers, "results", purpose[:200]))
        for r in fetcher.api(ACTIONS_URL.format(lo, hi), CACHE / "actions" / f"{lo:%Y%m}.json"):
            subject = (r.get("subject") or "").strip()
            fyers = ours(r.get("isin"), r.get("symbol"))
            if fyers and r.get("exDate") not in (None, "-") and not NOT_PRICE_EVENTS.search(subject):
                day = dt.datetime.strptime(r["exDate"], "%d-%b-%Y").date()
                rows.append((day, fyers, "ex_date", subject[:200]))
    df = pd.DataFrame(rows, columns=["day", "fyers_symbol", "kind", "detail"])
    return df[(df["day"] >= start) & (df["day"] <= end)].drop_duplicates(["day", "fyers_symbol", "kind"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=dt.date.fromisoformat, default=dt.date(2023, 9, 25))
    parser.add_argument("--end", type=dt.date.fromisoformat, default=dt.date.today() - dt.timedelta(days=1))
    args = parser.parse_args()

    members = pd.read_csv(MEMBERS, dtype=str, keep_default_na=False)
    members = members[members["fyers_symbol"] != ""]
    calendar = NseCalendar.load()
    days = [d for d in calendar.trading_days(args.start, args.end)]
    fetcher = Fetcher(HOME_URL)
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"daily status for {len(days)} trading days ({days[0]} to {days[-1]})")
    status, misses = daily_status(fetcher, members, days)
    status.to_parquet(OUT / "daily_status.parquet", index=False)
    print(f"wrote {len(status):,} member-days; {len(misses)} unmatched")
    if misses:
        counts = pd.DataFrame(misses, columns=["day", "fyers_symbol"]).groupby("fyers_symbol")["day"].agg(
            ["count", "min", "max"])
        print(counts.sort_values("count", ascending=False).to_string())
    print(f"series: {status['series'].value_counts().to_dict()}; "
          f"no band (F&O): {status['band_pct'].isna().mean():.0%}")

    events = corporate_events(fetcher, members, args.start, args.end)
    events.to_parquet(OUT / "corporate_events.parquet", index=False)
    print(f"wrote {len(events):,} corporate events: {events['kind'].value_counts().to_dict()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
