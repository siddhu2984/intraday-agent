"""News store: NSE filings on disk, one Parquet file per month of public time (data/news/filings_YYYY-MM.parquet).

Stored as fetched (agent.news.nse.parse). Everything derived — our FYERS symbol, point-in-time Nifty 500
membership, the news group — is added by `read`, so changing the classifier rules or the universe never needs a
re-download.
"""

from __future__ import annotations

import os
import re
from datetime import date

import pandas as pd

from agent.backtest.features import read_members
from agent.broker.fyers_auth import PROJECT_ROOT
from agent.news.classify import Classifier
from agent.news.nse import COLUMNS

DEFAULT_ROOT = PROJECT_ROOT / "data" / "news"


def _norm_company(name: str) -> str:
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    return " ".join(w for w in name.split() if w not in ("ltd", "limited", "the"))


class NewsStore:
    def __init__(self, root=DEFAULT_ROOT):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, month: str):
        return self.root / f"filings_{month}.parquet"

    def upsert(self, filings: pd.DataFrame) -> pd.DataFrame:
        """Add filings; returns the ones not seen before. An API row replaces an RSS row with the same PDF."""
        if filings.empty:
            return filings
        new_rows = []
        for month, part in filings.groupby(filings["public_ts"].dt.strftime("%Y-%m")):
            path = self._path(month)
            old = pd.read_parquet(path) if path.exists() else part.iloc[0:0]
            # An RSS row for a filing already stored from the API adds nothing.
            stored_api_pdfs = set(old.loc[old["source"] == "api", "pdf_url"]) - {""}
            part = part[~((part["source"] == "rss") & part["pdf_url"].isin(stored_api_pdfs))]
            # An API row replaces the RSS row of the same filing, but it isn't news any more.
            new_api_pdfs = set(part.loc[part["source"] == "api", "pdf_url"]) - {""}
            superseded = (old["source"] == "rss") & old["pdf_url"].isin(new_api_pdfs)
            seen_pdfs = set(old.loc[superseded, "pdf_url"])
            old = old[~superseded]
            fresh = part[~part["seq_id"].isin(set(old["seq_id"])) & ~part["pdf_url"].isin(seen_pdfs)]
            merged = pd.concat([old, part], ignore_index=True).drop_duplicates("seq_id", keep="last")
            merged = merged.sort_values(["public_ts", "seq_id"], ignore_index=True)[COLUMNS]
            tmp = path.with_name(path.name + ".tmp")
            merged.to_parquet(tmp, index=False)
            os.replace(tmp, path)
            new_rows.append(fresh)
        return pd.concat(new_rows, ignore_index=True)

    def months(self) -> list[str]:
        return sorted(p.stem.split("_")[1] for p in self.root.glob("filings_*.parquet"))

    def read_raw(self, start: date, end: date) -> pd.DataFrame:
        frames = [pd.read_parquet(self._path(m)) for m in self.months()
                  if f"{start:%Y-%m}" <= m <= f"{end:%Y-%m}"]
        if not frames:
            return pd.DataFrame(columns=COLUMNS)
        df = pd.concat(frames, ignore_index=True)
        day = df["public_ts"].dt.date
        return df[(day >= start) & (day <= end)].reset_index(drop=True)

    def read(self, start: date, end: date, members_only: bool = True,
             classifier: Classifier | None = None) -> pd.DataFrame:
        """Filings with fyers_symbol, day, member (point-in-time Nifty 500), sector and group added."""
        df = self.read_raw(start, end)
        return enrich(df, read_members(), classifier or Classifier.load(), members_only)


def enrich(df: pd.DataFrame, members: pd.DataFrame, classifier: Classifier, members_only: bool) -> pd.DataFrame:
    by_isin = dict(zip(members["isin"], members["fyers_symbol"]))
    by_symbol = dict(zip(members["symbol"], members["fyers_symbol"]))
    by_company = dict(zip(members["company"].map(_norm_company), members["fyers_symbol"]))
    df = df.copy()
    df["fyers_symbol"] = [by_isin.get(i) or by_symbol.get(s) or by_company.get(_norm_company(c))
                          for i, s, c in zip(df["isin"], df["symbol"], df["company"])]
    df["day"] = df["public_ts"].dt.date
    df["member"] = False
    df["sector"] = None
    for m in members.itertuples():
        rows = (df["fyers_symbol"] == m.fyers_symbol) & (df["day"] >= m.start)
        if m.end is not None and not pd.isna(m.end):
            rows &= df["day"] < m.end
        df.loc[rows, "member"] = True
        df.loc[rows, "sector"] = m.industry or None
    df["group"] = classifier.classify(df["category"], df["text"])
    return df[df["member"]].reset_index(drop=True) if members_only else df
