"""News store commands.

  python -m agent.news backfill [--start 2023-09-01] [--end today]
  python -m agent.news poll [--interval 30] [--once]
  python -m agent.news categories [--start ...]     # category/group counts for members, to tune the rules
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

from agent.broker.fyers_auth import IST
from agent.news.nse import AnnouncementsClient
from agent.news.store import NewsStore


def backfill(args) -> int:
    client, store = AnnouncementsClient(), NewsStore()
    end = args.end or datetime.now(IST).date()
    from agent.news.nse import months
    for lo, hi in months(args.start, end):
        filings = client.fetch_range(lo, hi)
        new = store.upsert(filings)
        print(f"{lo:%Y-%m}: {len(filings):,} filings, {len(new):,} new", flush=True)
    return 0


def categories(args) -> int:
    df = NewsStore().read(args.start, args.end or datetime.now(IST).date())
    counts = df.groupby(["group", "category"]).size().rename("n").reset_index().sort_values(
        ["group", "n"], ascending=[True, False])
    print(f"{len(df):,} member filings")
    print(df["group"].value_counts().to_string())
    print(counts.to_string(index=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.news")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backfill")
    b.add_argument("--start", type=date.fromisoformat, default=date(2023, 9, 1))
    b.add_argument("--end", type=date.fromisoformat)
    p = sub.add_parser("poll")
    p.add_argument("--interval", type=float, default=30)
    p.add_argument("--once", action="store_true", help="one poll, then exit")
    c = sub.add_parser("categories")
    c.add_argument("--start", type=date.fromisoformat, default=date(2023, 9, 1))
    c.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args(argv)
    if args.command == "backfill":
        return backfill(args)
    if args.command == "categories":
        return categories(args)
    from agent.news.poll import run as poll
    return poll(args.interval, args.once)


if __name__ == "__main__":
    sys.exit(main())
