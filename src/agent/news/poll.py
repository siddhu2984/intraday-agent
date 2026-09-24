"""Live news poller (architecture.md §4.3): today's NSE filings every `interval` seconds, 08:30–15:40 IST on
trading days. New filings go into the news store; material ones for Nifty 500 members are printed and logged with
their delay (our receive time − the exchange's public time). The RSS feed is the fallback when the JSON API fails.

No trading use yet: this measures latency and exercises the live path for the news-driven strategy.
"""

from __future__ import annotations

import logging
import time as time_mod
from datetime import datetime, time

from agent.backtest.features import read_members
from agent.broker.fyers_auth import IST, PROJECT_ROOT
from agent.news.classify import Classifier
from agent.news.nse import AnnouncementsClient
from agent.news.store import NewsStore, enrich
from agent.ops.calendar import NseCalendar

START, END = time(8, 30), time(15, 40)
IGNORED_GROUPS = {"noise", "other"}
LOG_PATH = PROJECT_ROOT / "data" / "logs" / "news_poll.log"

log = logging.getLogger("agent.news.poll")


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()])


def poll_once(client: AnnouncementsClient, store: NewsStore, members, classifier: Classifier,
              now: datetime) -> list[dict]:
    """One poll: fetch, store, and return the new material member filings with their delay in seconds."""
    try:
        filings = client.fetch_day(now.date())
    except Exception as exc:  # JSON API down or blocked → the RSS feed
        log.warning("announcements API failed (%s); using RSS", exc)
        filings = client.fetch_rss()
    new = store.upsert(filings)
    if new.empty:
        return []
    rows = enrich(new, members, classifier, members_only=True)
    rows = rows[~rows["group"].isin(IGNORED_GROUPS)]
    return [{"public_ts": r.public_ts, "delay_s": (now - r.public_ts).total_seconds(), "symbol": r.fyers_symbol,
             "group": r.group, "text": r.text, "source": r.source} for r in rows.itertuples()]


def run(interval: float = 30, once: bool = False) -> int:
    _setup_logging()
    calendar, client, store = NseCalendar.load(), AnnouncementsClient(), NewsStore()
    members, classifier = read_members(), Classifier.load()
    while True:
        now = datetime.now(IST)
        if not once:
            if not calendar.is_trading_day(now.date()) or now.time() >= END:
                log.info("outside trading hours (%s); stopping", now.strftime("%a %H:%M"))
                return 0
            if now.time() < START:
                time_mod.sleep(min(interval, (datetime.combine(now.date(), START, IST) - now).total_seconds()))
                continue
        for f in poll_once(client, store, members, classifier, now):
            log.info("%s %-22s %-14s delay %4.0fs [%s] %s", f["public_ts"].strftime("%H:%M:%S"), f["symbol"],
                     f["group"], f["delay_s"], f["source"], f["text"][:120])
        if once:
            return 0
        time_mod.sleep(interval)
