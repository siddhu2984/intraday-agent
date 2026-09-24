"""Entry point (architecture.md §7). Modes: backtest (phase 2); paper and live come in phase 3.

  python -m agent.main backtest [--start 2024-03-01] [--end 2024-03-31] [--workers 4]
                                [--set risk.leverage=5 ...] [--no-baseline] [--note "..."]

Writes data/backtests/<run_id>/: journal.db, trades.csv, baseline_trades.csv, report.md.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime

import pandas as pd

from agent.backtest.engine import DayResult, run
from agent.backtest.features import load_features
from agent.backtest.report import criteria, funnel, metrics, render, split_days, trades_frame
from agent.broker.fyers_auth import IST, PROJECT_ROOT
from agent.config import load_settings
from agent.data.store import CandleStore
from agent.journal.db import Journal, new_run_id
from agent.ops.calendar import NseCalendar

BACKTESTS_DIR = PROJECT_ROOT / "data" / "backtests"


def _periods(trades: pd.DataFrame, days: list[date], fraction: float, capital: float) -> dict[str, dict]:
    ins, val = split_days(days, fraction)
    return {"full": metrics(trades, days, capital), "in_sample": metrics(trades, ins, capital),
            "validation": metrics(trades, val, capital)}


def _journal(journal: Journal, results: list[DayResult]) -> None:
    journal.log_screen_results([s for r in results for s in r.screen])
    signals = [s for r in results for s in r.signals]
    journal.log_signals(signals)
    journal.log_risk_decisions([(s["intent"], s["decision"], s["ts"]) for s in signals if "decision" in s])
    journal.log_trades([t for r in results for t in r.trades])
    for r in results:
        for e in r.events:
            journal.log_event(e["kind"], "CRITICAL", e["message"], ts=e["ts"])


def backtest(args) -> int:
    settings = load_settings(overrides=args.set)
    start, end = args.start or settings.backtest.start, args.end or settings.backtest.end
    store = CandleStore()
    stored = set(store.days())
    store.close()
    days = [d for d in NseCalendar.load().trading_days(start, end) if d in stored]
    if not days:
        sys.exit(f"no stored trading days in {start} → {end}")
    t0 = time.time()
    features = load_features(start, end, settings.screener.lookback_days)
    print(f"{len(days)} days, features ready in {time.time() - t0:.0f} s", flush=True)

    run_id = new_run_id(datetime.now(IST))
    out = BACKTESTS_DIR / run_id
    out.mkdir(parents=True)
    journal = Journal(out / "journal.db")
    journal.start_run(settings, mode="backtest", note=args.note, run_id=run_id)

    t0 = time.time()
    results = run(settings, features, days, args.workers)
    trades = trades_frame(results)
    print(f"ORB: {len(trades)} trades in {time.time() - t0:.0f} s", flush=True)
    _journal(journal, results)
    journal.close()
    trades.to_csv(out / "trades.csv", index=False)
    fraction, capital = settings.backtest.validation_fraction, settings.capital
    periods = _periods(trades, days, fraction, capital)

    baseline: dict[int, dict] = {}
    fun = funnel(results)
    if not args.no_baseline:
        probability = fun["approved"] / max(1, fun["watchlist_symbol_days"])
        frames = []
        for seed in range(1, settings.backtest.baseline_seeds + 1):
            t0 = time.time()
            base_trades = trades_frame(run(settings, features, days, args.workers, seed=seed,
                                           probability=probability))
            print(f"baseline seed {seed}: {len(base_trades)} trades in {time.time() - t0:.0f} s", flush=True)
            baseline[seed] = _periods(base_trades, days, fraction, capital)
            frames.append(base_trades.assign(seed=seed))
        pd.concat(frames, ignore_index=True).to_csv(out / "baseline_trades.csv", index=False)

    checks = criteria(periods["validation"], [b["validation"] for b in baseline.values()])
    report = render(run_id, settings, days, periods["full"], periods["in_sample"], periods["validation"],
                    baseline, checks, fun, trades, args.note)
    (out / "report.md").write_text(report, encoding="utf-8")
    verdict = "PASS" if all(c.passed for c in checks) else "FAIL"
    print(f"\n{verdict}: " + "; ".join(f"{c.name} {c.value} {'ok' if c.passed else 'X'}" for c in checks))
    print(f"report: {out / 'report.md'}")
    return 0


def news_study(args) -> int:
    from datetime import timedelta

    from agent.news.store import NewsStore
    from agent.research.event_study import IGNORED_GROUPS, assign_sessions, pick_events, run_study
    from agent.research.study_report import render

    settings = load_settings(overrides=args.set)
    start, end = args.start or settings.backtest.start, args.end or settings.backtest.end
    calendar = NseCalendar.load()
    store = CandleStore()
    stored = set(store.days())
    store.close()
    days = [d for d in calendar.trading_days(start, end) if d in stored]
    features = load_features(start, end, settings.screener.lookback_days)
    filings = NewsStore().read(start - timedelta(days=7), end)
    filings = assign_sessions(filings[~filings["group"].isin(IGNORED_GROUPS)], calendar)
    events = pick_events(filings[filings["session_day"].isin(set(days))])
    print(f"{len(days)} days; {len(events):,} events", flush=True)

    t0 = time.time()
    df = run_study(events, features, days, args.workers)
    print(f"measured {len(df):,} rows in {time.time() - t0:.0f} s", flush=True)
    run_id = new_run_id(datetime.now(IST))
    out = PROJECT_ROOT / "data" / "research" / "news_study" / run_id
    out.mkdir(parents=True)
    df.to_parquet(out / "events.parquet", index=False)
    (out / "report.md").write_text(render(run_id, df, days, settings), encoding="utf-8")
    print(f"report: {out / 'report.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent")
    sub = parser.add_subparsers(dest="mode", required=True)
    bt = sub.add_parser("backtest", help="run the ORB backtest over stored history")
    bt.add_argument("--start", type=date.fromisoformat)
    bt.add_argument("--end", type=date.fromisoformat)
    bt.add_argument("--workers", type=int, default=None, help="processes (default: cores - 1)")
    bt.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="config override, repeatable")
    bt.add_argument("--no-baseline", action="store_true", help="skip the random-entry baseline")
    bt.add_argument("--note", default="", help="free text stored with the run")
    ns = sub.add_parser("news-study", help="event study: price follow-through after NSE filings")
    ns.add_argument("--start", type=date.fromisoformat)
    ns.add_argument("--end", type=date.fromisoformat)
    ns.add_argument("--workers", type=int, default=None)
    ns.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args(argv)
    return news_study(args) if args.mode == "news-study" else backtest(args)


if __name__ == "__main__":
    sys.exit(main())
