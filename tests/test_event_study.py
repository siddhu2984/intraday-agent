from datetime import date, datetime, time

import numpy as np
import pandas as pd
import pytest

from agent.broker.fyers_auth import IST
from agent.data.download import INDEX_SYMBOL
from agent.data.store import candles_to_frame
from agent.ops.calendar import NseCalendar
from agent.research.event_study import assign_sessions, pick_events, study_day
from agent.research.study_report import add_buckets, cost_pct, evaluate
from conftest import make_settings
from test_engine import path

DAY = date(2026, 9, 24)  # Thursday
CALENDAR = NseCalendar({date(2026, 10, 2): "Gandhi Jayanti"}, {})


def at(day, hh, mm, ss=0):
    return pd.Timestamp(datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=IST))


def bars():
    a = path(DAY, {"09:15": 100, "11:00": 100, "11:05": 101, "12:05": 102, "15:29": 102})
    b = path(DAY, {"09:15": 50, "15:29": 50.5})
    idx = path(DAY, {"09:15": 20_000, "15:29": 20_000})
    return pd.concat([candles_to_frame("NSE:A-EQ", a), candles_to_frame("NSE:B-EQ", b),
                      candles_to_frame(INDEX_SYMBOL, idx)], ignore_index=True)


def features():
    common = dict(day=DAY, member=True, prev_close=100.0, turnover_cr=50.0, or_volume=3000, or_volume_avg=1000.0,
                  sector="IT")
    return pd.DataFrame([{**common, "symbol": "NSE:A-EQ", "gap_pct": 1.0},
                         {**common, "symbol": "NSE:B-EQ", "gap_pct": -0.5, "prev_close": 50.0},
                         {**common, "symbol": INDEX_SYMBOL, "gap_pct": 0.2, "member": False, "sector": None}])


def events(*rows):
    return pd.DataFrame([dict(fyers_symbol=s, session_day=DAY, timing=t, group=g, public_ts=ts, text="x")
                         for s, t, g, ts in rows],
                        columns=["fyers_symbol", "session_day", "timing", "group", "public_ts", "text"])


def test_session_event_by_hand():
    ev = events(("NSE:A-EQ", "session", "order_win", at(DAY, 11, 0, 30)))
    rows = pd.DataFrame(study_day(DAY, ev, features(), bars()))
    e = rows[(rows["symbol"] == "NSE:A-EQ") & (rows["timing"] == "session")].iloc[0]
    assert e["news"] and e["group"] == "order_win" and e["minute"] == 105
    assert e["reaction_pct"] == pytest.approx(0.8)          # 100 → 100.8 at 11:04's close, index flat
    entry = 100.8                                            # 11:05 opens at 11:04's close
    assert e["ret_15m"] == pytest.approx((101 + 14 / 60 - entry) / entry * 100)   # close of 11:19
    assert e["ret_eod"] == pytest.approx((102 - entry) / entry * 100)             # 15:10 open
    assert e["adj_eod"] == pytest.approx(e["ret_eod"])      # index flat


def test_every_other_member_gets_controls():
    rows = pd.DataFrame(study_day(DAY, events(), features(), bars()))
    assert set(rows["symbol"]) == {"NSE:A-EQ", "NSE:B-EQ"}   # never the index
    assert not rows["news"].any()
    assert set(rows["timing"]) == {"session", "overnight"}
    b = rows[(rows["symbol"] == "NSE:B-EQ") & (rows["timing"] == "overnight")].iloc[0]
    assert b["reaction_pct"] == pytest.approx(-0.7)          # −0.5% gap vs the index's +0.2%
    entry, out = 50 + 0.5 * 14 / 374, 50 + 0.5 * 354 / 374  # opens at 09:30 and 15:10 = the previous closes
    assert b["ret_0930_eod"] == pytest.approx(-(out / entry - 1) * 100)  # short: the gap was down


def test_overnight_event_is_labelled():
    ev = events(("NSE:A-EQ", "overnight", "results", at(date(2026, 9, 23), 18, 0)))
    rows = pd.DataFrame(study_day(DAY, ev, features(), bars()))
    a = rows[(rows["symbol"] == "NSE:A-EQ") & (rows["timing"] == "overnight")].iloc[0]
    assert a["news"] and a["group"] == "results" and a["spike"] == 3


@pytest.mark.parametrize("ts, day, timing", [
    (at(DAY, 10, 0), DAY, "session"),
    (at(DAY, 9, 20), DAY, "session"),
    (at(DAY, 8, 0), DAY, "overnight"),
    (at(DAY, 16, 0), date(2026, 9, 25), "overnight"),
    (at(date(2026, 9, 26), 11, 0), date(2026, 9, 28), "overnight"),   # Saturday → Monday
    (at(date(2026, 10, 1), 19, 0), date(2026, 10, 5), "overnight"),   # before a holiday + weekend
    (at(DAY, 9, 17), None, None),
    (at(DAY, 15, 10), None, None),
])
def test_assign_sessions(ts, day, timing):
    out = assign_sessions(pd.DataFrame({"public_ts": [ts]}), CALENDAR)
    if timing is None:
        assert out.empty
    else:
        assert (out.loc[0, "session_day"], out.loc[0, "timing"]) == (day, timing)


def test_pick_events_keeps_the_most_material_overnight_and_the_first_session_filing():
    f = pd.DataFrame([
        dict(fyers_symbol="A", session_day=DAY, timing="overnight", group="press_release", public_ts=at(DAY, 7, 0)),
        dict(fyers_symbol="A", session_day=DAY, timing="overnight", group="results", public_ts=at(DAY, 8, 0)),
        dict(fyers_symbol="A", session_day=DAY, timing="session", group="results", public_ts=at(DAY, 13, 0)),
        dict(fyers_symbol="A", session_day=DAY, timing="session", group="dividend", public_ts=at(DAY, 10, 0)),
    ]).assign(category="c", text="t")
    out = pick_events(f).set_index("timing")
    assert out.loc["overnight", "group"] == "results"
    assert out.loc["session", "group"] == "dividend"


def test_cost_and_shortlist():
    settings = make_settings()
    cost = cost_pct(settings)
    assert 0.1 < cost < 0.25            # ~0.06% charges + 2 × 5 bps slippage
    rng = np.random.default_rng(1)
    days = [date(2024, 1, 1) + pd.Timedelta(days=i) for i in range(300)]

    def rows(n, mean, news, group):
        return pd.DataFrame({"timing": "session", "news": news, "group": group,
                             "day": [days[i % 300] for i in range(n)],
                             "reaction_pct": rng.normal(0, 1, n), "ret_15m": rng.normal(mean, 0.5, n),
                             "ret_60m": rng.normal(mean, 0.5, n), "ret_eod": rng.normal(mean, 0.5, n)})

    df = pd.concat([rows(3000, 0.0, False, "none"), rows(600, 0.6, True, "order_win"),
                    rows(600, 0.0, True, "press_release")], ignore_index=True)
    table = evaluate(add_buckets(df), cost, set(days[:200]))
    table = table[table["horizon"] == "eod"].set_index("group")
    assert table.loc["order_win", "shortlist"] and not table.loc["press_release", "shortlist"]
    assert table.loc["order_win", "edge"] == pytest.approx(0.6, abs=0.1)
