from datetime import datetime, timedelta

import pandas as pd
import pytest

from agent.backtest.costs import leg_costs, round_trip
from agent.broker.fyers_auth import IST
from agent.data.indicators import opening_range, resample, session_vwap, wilder_atr
from agent.risk.state import Side
from conftest import make_settings

COSTS = make_settings().costs


# --- costs ---

def test_buy_leg_below_the_brokerage_cap():
    c = leg_costs(19_674.50, buy=True, c=COSTS)    # 38 × 517.75, the stock-selection.md example
    assert c.brokerage == pytest.approx(5.90235)    # 0.03%
    assert c.stt == 0
    assert c.exchange == pytest.approx(0.58433265)  # 0.00297%
    assert c.sebi == pytest.approx(0.0196745)
    assert c.stamp == pytest.approx(0.590235)       # 0.003%, buy only
    assert c.gst == pytest.approx(0.18 * (5.90235 + 0.58433265 + 0.0196745))


def test_sell_leg_pays_stt_not_stamp():
    c = leg_costs(20_035.50, buy=False, c=COSTS)
    assert c.stt == pytest.approx(5.008875)          # 0.025%
    assert c.stamp == 0


def test_brokerage_is_capped():
    assert leg_costs(200_000, buy=True, c=COSTS).brokerage == 20


def test_round_trip_sides():
    long = round_trip(Side.LONG, 38, 517.75, 527.25, COSTS)
    assert long.total == pytest.approx(leg_costs(19_674.5, True, COSTS).total + leg_costs(20_035.5, False, COSTS).total)
    assert 20 < long.total < 30                      # ~0.06% of turnover per side at this size
    short = round_trip(Side.SHORT, 38, 527.25, 517.75, COSTS)
    assert short.stt == pytest.approx(20_035.5 * 0.00025)  # a short sells at entry
    assert short.stamp == pytest.approx(19_674.5 * 0.00003)


# --- indicators ---

def minute_bars(closes, start=datetime(2026, 9, 24, 9, 15, tzinfo=IST), volume=100):
    ts = [start + timedelta(minutes=i) for i in range(len(closes))]
    return pd.DataFrame({"ts": ts, "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
                         "close": closes, "volume": volume})


def test_resample_to_5_min_boundaries():
    bars = minute_bars(list(range(100, 112)))           # 09:15 … 09:26
    c = resample(bars)
    assert [t.strftime("%H:%M") for t in c["ts"]] == ["09:15", "09:20", "09:25"]
    first = c.iloc[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (100, 105, 99, 104, 500)
    assert c.iloc[-1].volume == 200                      # partial last bucket


def test_resample_skips_the_overnight_gap():
    prev = minute_bars([100] * 10, start=datetime(2026, 9, 23, 15, 20, tzinfo=IST))
    today = minute_bars([101] * 5)
    c = resample(pd.concat([prev, today], ignore_index=True))
    assert [t.strftime("%m-%d %H:%M") for t in c["ts"]] == ["09-23 15:20", "09-23 15:25", "09-24 09:15"]


def test_session_vwap():
    bars = minute_bars([100, 110])
    bars["volume"] = [100, 300]
    assert session_vwap(bars).tolist() == pytest.approx([100, (100 * 100 + 110 * 300) / 400])
    bars["volume"] = 0
    assert session_vwap(bars).isna().all()


def test_wilder_atr_by_hand():
    candles = pd.DataFrame({"high": [10, 12, 11, 13], "low": [8, 9, 9, 10], "close": [9, 11, 10, 12]})
    # true ranges: 2, max(3, 3, 0)=3, max(2, 0, 2)=2, max(3, 3, 0)=3
    atr = wilder_atr(candles, period=2)
    assert pd.isna(atr[0])
    assert atr[1:].tolist() == pytest.approx([2.5, (2.5 + 2) / 2, (2.25 + 3) / 2])


def test_opening_range():
    bars = minute_bars([100, 102, 99, 101] + [120] * 20)
    assert opening_range(bars, bars["ts"][0], 15) == (121, 98)  # the 120s from 09:19 are inside the window
    assert opening_range(bars, bars["ts"][0], 4) == (103, 98)
