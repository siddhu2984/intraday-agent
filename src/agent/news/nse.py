"""NSE corporate announcements — company filings with their exact public time (architecture.md §4.3).

Sources:
  JSON  www.nseindia.com/api/corporate-announcements — full history, one row per filing with a unique seq_id,
        ISIN, category and both times: `an_dt` (the company's submission) and `exchdisstime` (the exchange's
        broadcast = when the market could see it; usually 0–7 s later). Month ranges come back complete.
  RSS   nsearchives.nseindia.com/content/RSS/Online_announcements.xml — the latest filings, refreshed every
        5 min; no ISIN or seq_id. Only a fallback for the live poller when the JSON API fails.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta

import pandas as pd

from agent.broker.fyers_auth import IST, PROJECT_ROOT
from agent.data.nse_http import Fetcher

API_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities&from_date={:%d-%m-%Y}&to_date={:%d-%m-%Y}"
RSS_URL = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
CACHE = PROJECT_ROOT / "data" / "nse_archives" / "announcements"
COLUMNS = ["seq_id", "symbol", "isin", "company", "category", "text", "pdf_url", "company_ts", "public_ts",
           "industry", "source"]
TIME_FORMAT = "%d-%b-%Y %H:%M:%S"


def _ts(value) -> pd.Timestamp:
    if not value or value == "-":
        return pd.NaT
    try:
        return pd.Timestamp(datetime.strptime(value.strip(), TIME_FORMAT).replace(tzinfo=IST))
    except ValueError:
        return pd.NaT


def _empty() -> pd.DataFrame:
    df = pd.DataFrame(columns=COLUMNS)
    for c in ("company_ts", "public_ts"):
        df[c] = pd.Series(dtype=f"datetime64[s, {IST}]")
    return df


def parse(rows: list[dict]) -> pd.DataFrame:
    """API rows → one row per filing. public_ts = exchange broadcast time, or the company's time if missing."""
    if not rows:
        return _empty()
    out = pd.DataFrame({
        "seq_id": [str(r.get("seq_id") or "") for r in rows],
        "symbol": [(r.get("symbol") or "").strip() for r in rows],
        "isin": [(r.get("sm_isin") or "").strip() for r in rows],
        "company": [(r.get("sm_name") or "").strip() for r in rows],
        "category": [(r.get("desc") or "").strip() for r in rows],
        "text": [(r.get("attchmntText") or "").strip() for r in rows],
        "pdf_url": [(r.get("attchmntFile") or "").strip() for r in rows],
        "company_ts": [_ts(r.get("an_dt")) for r in rows],
        "public_ts": [_ts(r.get("exchdisstime")) for r in rows],
        "industry": [(r.get("smIndustry") or "").strip() for r in rows],
        "source": "api",
    })
    out["public_ts"] = out["public_ts"].fillna(out["company_ts"])
    for c in ("company_ts", "public_ts"):
        out[c] = pd.to_datetime(out[c]).astype(f"datetime64[s, {IST}]")
    return out[out["seq_id"] != ""].drop_duplicates("seq_id", keep="last").reset_index(drop=True)


def parse_rss(xml: bytes) -> pd.DataFrame:
    """RSS items → the same columns. seq_id is 'rss:' + a hash of the PDF link; symbol and ISIN are unknown."""
    rows = []
    for item in ET.fromstring(xml).iter("item"):
        link = (item.findtext("link") or "").strip()
        description = (item.findtext("description") or "").strip()
        text, _, subject = description.partition("|SUBJECT:")
        rows.append({
            "seq_id": "rss:" + hashlib.sha1(link.encode()).hexdigest()[:16], "symbol": "", "isin": "",
            "company": (item.findtext("title") or "").strip(), "category": subject.strip(), "text": text.strip(),
            "pdf_url": link, "company_ts": _ts(item.findtext("pubDate")), "public_ts": _ts(item.findtext("pubDate")),
            "industry": "", "source": "rss",
        })
    if not rows:
        return _empty()
    out = pd.DataFrame(rows, columns=COLUMNS)
    for c in ("company_ts", "public_ts"):
        out[c] = pd.to_datetime(out[c]).astype(f"datetime64[s, {IST}]")
    return out


def months(start: date, end: date) -> list[tuple[date, date]]:
    result, cur = [], start.replace(day=1)
    while cur <= end:
        nxt = (cur + timedelta(days=32)).replace(day=1)
        result.append((max(cur, start), min(nxt - timedelta(days=1), end)))
        cur = nxt
    return result


class AnnouncementsClient:
    def __init__(self, fetcher: Fetcher | None = None, cache_dir=CACHE):
        self.fetcher = fetcher or Fetcher()
        self.cache_dir = cache_dir

    def _rows(self, start: date, end: date, cacheable: bool) -> list[dict]:
        path = self.cache_dir / f"{start:%Y%m%d}_{end:%Y%m%d}.json.gz"
        if cacheable and path.exists():
            return json.loads(gzip.decompress(path.read_bytes()))
        rows = self.fetcher.api(API_URL.format(start, end))
        if not isinstance(rows, list):
            raise RuntimeError(f"unexpected response for {start}..{end}: {str(rows)[:200]}")
        if cacheable:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(gzip.compress(json.dumps(rows).encode()))
        return rows

    def fetch_range(self, start: date, end: date, today: date | None = None) -> pd.DataFrame:
        """All filings with start <= day <= end, one request per calendar month. Months that ended before today
        are cached (a filing can't be added to a finished month)."""
        today = today or datetime.now(IST).date()
        frames = [parse(self._rows(lo, hi, cacheable=hi < today)) for lo, hi in months(start, end)]
        return pd.concat(frames, ignore_index=True) if frames else _empty()

    def fetch_day(self, day: date) -> pd.DataFrame:
        return parse(self._rows(day, day, cacheable=False))

    def fetch_rss(self) -> pd.DataFrame:
        data = self.fetcher.get(RSS_URL)
        return parse_rss(data) if data else _empty()
