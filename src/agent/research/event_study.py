"""News event study: after which filings does the price keep moving enough to beat costs?

For each Nifty 500 member and trading day:

  Session event   a material filing made public at T, 09:20 <= T < 15:00. Reaction = the move from the last close
                  before T to the close 5 minutes later, net of NIFTY 50. The follow-through trade enters at the
                  next bar's open in the reaction's direction and exits at +15 min, +60 min, or 15:10.
  Overnight event a material filing made public after the previous session's 15:30 (or before 09:15). Reaction =
                  the opening gap net of NIFTY 50. The trade enters at 09:20 or 09:30 in the gap's direction and
                  exits at +60 min or 15:10.

Controls are the same measurements where there was no material filing: every other member stock-day for the
overnight case, and one seeded random time per stock-day for the session case. Comparing within the same
reaction-size bucket shows what news adds beyond "stocks that moved keep moving".

Returns are percentages in the trade's direction: `ret_*` is what the trade makes, `adj_*` is net of NIFTY 50.
Costs (FYERS charges on a ₹20k position + slippage on both legs) turn gross into net in the report.
One event per stock and day per timing; when several filings qualify, the most material group (GROUP_PRIORITY)
names the event.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, time

import numpy as np
import pandas as pd

from agent.broker.fyers_auth import IST
from agent.data.download import INDEX_SYMBOL
from agent.data.store import CandleStore
from agent.ops.calendar import NseCalendar

GROUP_PRIORITY = ["results", "order_win", "mna", "regulatory", "usfda", "auditor", "credit_rating", "management",
                  "fund_raising", "capital_return", "operations", "business_update", "deal", "exchange_query",
                  "board_outcome", "press_release", "dividend"]
IGNORED_GROUPS = {"noise", "other"}
SESSION_FIRST, SESSION_LAST = time(9, 20), time(15, 0)
REACTION_MIN = 5
EXIT_MINUTE = 355          # 15:10 = minute 355 after 09:15
SESSION_HORIZONS = {"15m": 15, "60m": 60}
OVERNIGHT_ENTRIES = {"0920": 5, "0930": 15}


def _minute(ts: pd.Timestamp, day: date) -> int:
    return int((ts - datetime.combine(day, time(9, 15), IST)).total_seconds() // 60)


def _grids(bars: pd.DataFrame, day: date) -> tuple[list[str], dict[str, np.ndarray]]:
    """symbol × 375-minute grids: open (missing → last close), close (forward-filled), volume (0 if missing)."""
    minute = ((bars["ts"] - pd.Timestamp(datetime.combine(day, time(9, 15), IST))).dt.total_seconds() // 60)
    bars = bars.assign(m=minute.astype(int))
    bars = bars[(bars["m"] >= 0) & (bars["m"] < 375)]
    symbols = sorted(bars["symbol"].unique())
    idx = {s: i for i, s in enumerate(symbols)}
    rows, cols = bars["symbol"].map(idx).to_numpy(), bars["m"].to_numpy()
    grids = {}
    for name in ("open", "close", "volume"):
        g = np.full((len(symbols), 375), np.nan)
        g[rows, cols] = bars[name].to_numpy(dtype=float)
        grids[name] = g
    close = pd.DataFrame(grids["close"]).ffill(axis=1).to_numpy()
    prev_close = np.concatenate([np.full((len(symbols), 1), np.nan), close[:, :-1]], axis=1)
    grids["open"] = np.where(np.isnan(grids["open"]), prev_close, grids["open"])
    grids["close"] = close
    grids["volume"] = np.nan_to_num(grids["volume"])
    return symbols, grids


def _directional(g: dict, i: int, j: int, entry_m: int, exit_m: int, sign: float, exit_at_open: bool) -> tuple:
    """(raw %, market-adjusted %) for a trade entered at the open of minute entry_m, exited at the close of
    minute exit_m − 1 (or the open of exit_m for the 15:10 exit)."""
    exit_grid = g["open"] if exit_at_open else g["close"]
    exit_col = exit_m if exit_at_open else exit_m - 1
    entry, out = g["open"][i, entry_m], exit_grid[i, exit_col]
    ientry, iout = g["open"][j, entry_m], exit_grid[j, exit_col]
    raw = sign * (out / entry - 1) * 100
    return raw, raw - sign * (iout / ientry - 1) * 100


def _session_row(g, i, j, k, per_min_vol) -> dict | None:
    """Metrics for a session event at minute index k (T within minute k)."""
    if k < 5 or k + REACTION_MIN >= EXIT_MINUTE:
        return None
    r = k + REACTION_MIN
    p0, pr, i0, ir = g["close"][i, k - 1], g["close"][i, r - 1], g["close"][j, k - 1], g["close"][j, r - 1]
    reaction = ((pr / p0 - 1) - (ir / i0 - 1)) * 100
    if not np.isfinite(reaction) or reaction == 0:
        return None
    sign = float(np.sign(reaction))
    row = {"minute": k, "reaction_pct": reaction, "spike": g["volume"][i, k:r].sum() / REACTION_MIN / per_min_vol
           if per_min_vol else np.nan}
    for name, h in SESSION_HORIZONS.items():
        row[f"ret_{name}"], row[f"adj_{name}"] = _directional(g, i, j, r, min(r + h, EXIT_MINUTE), sign, False)
    row["ret_eod"], row["adj_eod"] = _directional(g, i, j, r, EXIT_MINUTE, sign, True)
    return row


def _overnight_row(g, i, j, gap_adj: float) -> dict | None:
    if not np.isfinite(gap_adj) or gap_adj == 0:
        return None
    sign = float(np.sign(gap_adj))
    row = {"reaction_pct": gap_adj}
    for name, m in OVERNIGHT_ENTRIES.items():
        row[f"ret_{name}_60m"], row[f"adj_{name}_60m"] = _directional(g, i, j, m, m + 60, sign, False)
        row[f"ret_{name}_eod"], row[f"adj_{name}_eod"] = _directional(g, i, j, m, EXIT_MINUTE, sign, True)
    return row


def assign_sessions(filings: pd.DataFrame, calendar: NseCalendar) -> pd.DataFrame:
    """Each material filing → (session day, timing): 'session' if public during 09:20–15:00 of a trading day,
    'overnight' for the next session if public after the previous 15:30 or before 09:15; others dropped."""
    days, timings = [], []
    for ts in filings["public_ts"]:
        d, t = ts.date(), ts.time()
        trading = calendar.is_trading_day(d)
        if trading and SESSION_FIRST <= t < SESSION_LAST:
            day, timing = d, "session"
        elif trading and t < time(9, 15):
            day, timing = d, "overnight"
        elif not trading or t >= time(15, 30):
            day, timing = calendar.next_trading_day(d), "overnight"
        else:  # 09:15–09:20 or 15:00–15:30: too little data before, or time after, to measure
            day, timing = None, None
        days.append(day)
        timings.append(timing)
    df = filings.assign(session_day=days, timing=timings)
    return df[df["timing"].notna()].reset_index(drop=True)


def pick_events(filings: pd.DataFrame) -> pd.DataFrame:
    """One event per (symbol, session day, timing): the most material group; the earliest time for sessions."""
    rank = {g: i for i, g in enumerate(GROUP_PRIORITY)}
    df = filings.assign(rank=filings["group"].map(rank).fillna(len(rank)))
    session = df[df["timing"] == "session"].sort_values("public_ts").drop_duplicates(["fyers_symbol", "session_day"])
    overnight = df[df["timing"] == "overnight"].sort_values(["rank", "public_ts"]).drop_duplicates(
        ["fyers_symbol", "session_day"])
    cols = ["fyers_symbol", "session_day", "timing", "group", "public_ts", "category", "text"]
    return pd.concat([session, overnight], ignore_index=True)[cols]


def study_day(day: date, events: pd.DataFrame, features_day: pd.DataFrame, bars: pd.DataFrame,
              seed: int = 0) -> list[dict]:
    """Event and control rows for one session day."""
    symbols, g = _grids(bars, day)
    pos = {s: i for i, s in enumerate(symbols)}
    if INDEX_SYMBOL not in pos:
        return []
    j = pos[INDEX_SYMBOL]
    feat = features_day.set_index("symbol")
    index_gap = feat.loc[INDEX_SYMBOL, "gap_pct"] if INDEX_SYMBOL in feat.index else np.nan
    members = [s for s in feat.index[feat["member"]] if s in pos]
    rows = []
    session_events = events[events["timing"] == "session"].set_index("fyers_symbol")
    overnight_events = events[events["timing"] == "overnight"].set_index("fyers_symbol")
    rng = np.random.default_rng([seed, day.toordinal()])

    for s in members:
        i, f = pos[s], feat.loc[s]
        per_min_vol = f["turnover_cr"] * 1e7 / f["prev_close"] / 375 if pd.notna(f["turnover_cr"]) else np.nan
        base = {"day": day, "symbol": s, "sector": f["sector"]}
        # overnight: every member is either an event or a control
        on = _overnight_row(g, i, j, f["gap_pct"] - index_gap)
        if on is not None:
            on["spike"] = f["or_volume"] / f["or_volume_avg"] if f["or_volume_avg"] else np.nan
            if s in overnight_events.index:
                e = overnight_events.loc[s]
                rows.append({**base, **on, "timing": "overnight", "news": True, "group": e["group"],
                             "public_ts": e["public_ts"], "text": e["text"]})
            else:
                rows.append({**base, **on, "timing": "overnight", "news": False, "group": "none"})
        # session: the filing's minute, or a random minute for a control
        if s in session_events.index:
            e = session_events.loc[s]
            row = _session_row(g, i, j, _minute(e["public_ts"], day), per_min_vol)
            if row is not None:
                rows.append({**base, **row, "timing": "session", "news": True, "group": e["group"],
                             "public_ts": e["public_ts"], "text": e["text"]})
        else:
            row = _session_row(g, i, j, int(rng.integers(5, 345)), per_min_vol)
            if row is not None:
                rows.append({**base, **row, "timing": "session", "news": False, "group": "none"})
    return rows


def _run_days(days: list[date], events: pd.DataFrame, features: pd.DataFrame) -> list[dict]:
    store = CandleStore()
    try:
        by_day_events = dict(tuple(events.groupby("session_day")))
        by_day_features = dict(tuple(features.groupby("day")))
        rows = []
        for d in days:
            if d not in by_day_features or not store.day_path(d).exists():
                continue
            ev = by_day_events.get(d, events.iloc[0:0])
            rows += study_day(d, ev, by_day_features[d], store.read_day(d))
        return rows
    finally:
        store.close()


def run_study(events: pd.DataFrame, features: pd.DataFrame, days: list[date], workers: int | None = None
              ) -> pd.DataFrame:
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    size = max(5, len(days) // (workers * 4))
    chunks = [days[i:i + size] for i in range(0, len(days), size)]
    if workers == 1 or len(chunks) == 1:
        rows = _run_days(days, events, features)
    else:
        with ProcessPoolExecutor(workers) as pool:
            futures = [pool.submit(_run_days, c, events[events["session_day"].isin(set(c))],
                                   features[features["day"].isin(set(c))]) for c in chunks]
            rows = [r for f in futures for r in f.result()]
    return pd.DataFrame(rows).sort_values(["day", "timing", "symbol"], ignore_index=True)

