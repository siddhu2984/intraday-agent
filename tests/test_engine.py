"""End-to-end days on a synthetic candle store: screener → ORB → gate → SimBroker → trade manager."""

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from agent.backtest.engine import run_day
from agent.backtest.report import criteria, metrics, split_days
from agent.broker.fyers_auth import IST
from agent.data.download import INDEX_SYMBOL
from agent.data.indicators import opening_range
from agent.data.store import CandleStore, candles_to_frame
from agent.ops.calendar import NseCalendar
from agent.strategy.orb import RandomEntry
from conftest import make_settings

DAY, PREV = date(2026, 9, 24), date(2026, 9, 23)
CALENDAR = NseCalendar({date(2026, 1, 26): "Republic Day"}, {})
SETTINGS = make_settings()
OPENING = {"09:15": 100.0, "09:20": 101.0, "09:29": 100.6}   # opening range ≈ 99.95 – 101.05
BREAKOUT = {**OPENING, "09:34": 101.6}                      # first 5-min candle after the range closes above it


def path(day: date, keypoints: dict[str, float], wick: float = 0.05) -> list:
    """1-min FYERS-style candles along straight lines between keypoints ('HH:MM' → price), 09:15–15:29."""
    minutes = [datetime.combine(day, time(9, 15), IST) + timedelta(minutes=i) for i in range(375)]
    known = {datetime.combine(day, time.fromisoformat(k), IST): v for k, v in keypoints.items()}
    xs = [(m - minutes[0]).total_seconds() for m in sorted(known)]
    closes = np.interp([(m - minutes[0]).total_seconds() for m in minutes], xs, [known[k] for k in sorted(known)])
    candles, prev = [], closes[0]
    for m, c in zip(minutes, closes):
        candles.append([int(m.timestamp()), prev, max(prev, c) + wick, min(prev, c) - wick, c, 1000])
        prev = c
    return candles


def write_store(root, symbols: dict[str, dict[str, float]]) -> CandleStore:
    store = CandleStore(root)
    prev = [candles_to_frame(s, path(PREV, {"09:15": 100, "15:29": 100}, wick=0.5)) for s in symbols]
    store.write_day(PREV, pd.concat(prev, ignore_index=True), [])
    today = [candles_to_frame(s, path(DAY, kp)) for s, kp in symbols.items()]
    today.append(candles_to_frame(INDEX_SYMBOL, path(DAY, {"09:15": 20_000, "15:29": 20_000})))
    store.write_day(DAY, pd.concat(today, ignore_index=True), [])
    return store


def features(store: CandleStore, sectors: dict[str, str | None], **flags) -> pd.DataFrame:
    """What the features table would hold for DAY (prev close 100, 3× opening volume, liquid, 20% band)."""
    bars = store.read_day(DAY)
    rows = []
    for symbol, g in bars.groupby("symbol"):
        high, low = opening_range(g, g["ts"].iloc[0], 15)
        or_close = float(g[g["ts"].dt.time < time(9, 30)]["close"].iloc[-1])
        index = symbol == INDEX_SYMBOL
        rows.append(dict(day=DAY, symbol=symbol, member=not index, open=float(g["open"].iloc[0]),
                         prev_close=20_000.0 if index else 100.0, gap_pct=0.0, turnover_cr=50.0, atr_pct=2.0,
                         series="EQ", band_pct=20.0, or_high=high, or_low=low, or_close=or_close,
                         or_volume=15_000, or_volume_avg=5_000.0, or_bars=15,
                         results=flags.get("results", False), ex_date=False,
                         sector=None if index else sectors.get(symbol, "IT")))
    return pd.DataFrame(rows)


def day_run(tmp_path, symbols, sectors=None, settings=SETTINGS, strategy=None, **flags):
    store = write_store(tmp_path, symbols)
    try:
        return run_day(DAY, features(store, sectors or {}, **flags), settings, CALENDAR, store, strategy)
    finally:
        store.close()


@pytest.mark.parametrize("after, reason", [
    ({"10:30": 104.5, "15:29": 104.5}, "target"),
    ({"10:00": 99.5, "15:29": 99.5}, "stop"),
    ({"10:00": 103.0, "11:00": 100.5, "15:29": 100.5}, "breakeven"),
    ({"15:29": 102.0}, "time_exit"),
])
def test_long_trade_exits(tmp_path, after, reason):
    result = day_run(tmp_path, {"NSE:AAA-EQ": {**BREAKOUT, "09:35": 101.6, **after}})
    [t] = result.trades
    assert t["side"] == "long" and t["exit_reason"] == reason
    assert t["entry_ts"].strftime("%H:%M") == "09:35"
    assert t["entry_price"] == pytest.approx(101.6)          # limit 101.65, the bar opened at 101.60
    assert t["stop"] == pytest.approx(100.55)                # breakout candle low (under the 1.5 × ATR cap)
    assert t["qty"] == 196 and t["binding"] == "position_cap"  # floor(20% of ₹1 lakh / 101.65)
    slip = SETTINGS.backtest.slippage_bps / 10_000
    expected_exit = {"target": t["target"], "stop": 100.55 * (1 - slip), "breakeven": 101.6 * (1 - slip)}
    if reason in expected_exit:
        assert t["exit_price"] == pytest.approx(expected_exit[reason])
    else:
        assert t["exit_ts"].strftime("%H:%M") == "15:10"
    assert t["pnl_net"] == pytest.approx(t["pnl_gross"] - t["costs"])
    assert t["r_multiple"] == pytest.approx(t["pnl_net"] / (196 * (101.6 - 100.55)))


def test_short_trade(tmp_path):
    down = {"09:15": 100.0, "09:20": 99.0, "09:29": 99.4, "09:34": 98.4, "09:35": 98.4, "10:30": 95.0,
            "15:29": 95.0}
    [t] = day_run(tmp_path, {"NSE:BBB-EQ": down}).trades
    assert (t["side"], t["exit_reason"]) == ("short", "target")
    assert t["pnl_gross"] > 0


def test_no_breakout_no_trade(tmp_path):
    result = day_run(tmp_path, {"NSE:AAA-EQ": {**OPENING, "15:29": 100.5}})
    assert result.trades == [] and result.signals == []
    assert [s["stage"] for s in result.screen] == ["A", "B"]


def test_results_day_is_hard_blocked(tmp_path):
    result = day_run(tmp_path, {"NSE:AAA-EQ": {**BREAKOUT, "15:29": 104}}, results=True)
    assert result.trades == []
    [s] = result.signals
    assert s["outcome"] == "blocked" and "results" in s["detail"]


def test_gate_rejection_is_recorded(tmp_path):
    result = day_run(tmp_path, {"NSE:AAA-EQ": {**BREAKOUT, "15:29": 104}}, sectors={"NSE:AAA-EQ": None})
    [s] = result.signals
    assert result.trades == [] and s["outcome"] == "rejected" and "sector unknown" in s["detail"]


def test_three_losses_engage_the_kill_switch(tmp_path):
    loser = {**BREAKOUT, "09:35": 101.6, "09:50": 99.0, "15:29": 99.0}
    late = {**OPENING, "10:24": 100.7, "10:29": 101.8, "15:29": 104}   # breaks out after the three losses
    symbols = {"NSE:L1-EQ": loser, "NSE:L2-EQ": loser, "NSE:L3-EQ": loser, "NSE:LATE-EQ": late}
    sectors = {"NSE:L1-EQ": "A", "NSE:L2-EQ": "B", "NSE:L3-EQ": "C", "NSE:LATE-EQ": "D"}
    result = day_run(tmp_path, symbols, sectors)
    assert [t["exit_reason"] for t in result.trades] == ["stop"] * 3
    assert [e["kind"] for e in result.events] == ["KILL_SWITCH"]
    late_signal = next(s for s in result.signals if s["symbol"] == "NSE:LATE-EQ")
    assert late_signal["outcome"] == "rejected" and late_signal["detail"] == "kill switch engaged"


def test_same_inputs_same_trades(tmp_path):
    symbols = {"NSE:AAA-EQ": {**BREAKOUT, "10:00": 103.0, "11:00": 100.5, "15:29": 100.5}}
    first = day_run(tmp_path / "a", symbols).trades
    assert first == day_run(tmp_path / "b", symbols).trades


def test_random_baseline_is_seeded(tmp_path):
    symbols = {"NSE:AAA-EQ": {**OPENING, "12:00": 103.0, "15:29": 101.0}}
    calm = make_settings(strategy={"min_stop_pct": 0.01})  # this path's candles are narrower than 0.3%
    runs = [day_run(tmp_path / str(i), symbols, settings=calm, strategy=RandomEntry(seed, 1.0)).signals
            for i, seed in enumerate((7, 7, 8))]
    assert runs[0] and runs[0][0]["ts"] == runs[1][0]["ts"]
    assert all(s["reason"].startswith("random entry") for s in runs[2])


# --- report metrics ---

def test_metrics_and_criteria():
    days = [date(2026, 9, d) for d in (21, 22, 23, 24)]
    trades = pd.DataFrame([
        dict(day=days[0], pnl_net=300.0, pnl_gross=330.0, costs=30.0, r_multiple=1.5, risk_amount=200.0,
             binding="position_cap", exit_reason="target"),
        dict(day=days[1], pnl_net=-220.0, pnl_gross=-200.0, costs=20.0, r_multiple=-1.1, risk_amount=200.0,
             binding="position_cap", exit_reason="stop"),
        dict(day=days[1], pnl_net=-100.0, pnl_gross=-80.0, costs=20.0, r_multiple=-0.5, risk_amount=200.0,
             binding="risk", exit_reason="time_exit"),
        dict(day=days[3], pnl_net=500.0, pnl_gross=530.0, costs=30.0, r_multiple=2.5, risk_amount=200.0,
             binding="risk", exit_reason="target"),
    ])
    m = metrics(trades, days, capital=100_000)
    assert m["trades"] == 4 and m["win_rate"] == 50
    assert m["expectancy_r"] == pytest.approx(0.6)
    assert m["profit_factor"] == pytest.approx(800 / 320)
    assert m["max_dd"] == pytest.approx(320)                  # +300, then −320 on day 2
    assert m["cap_bound_pct"] == 50 and m["exits"] == {"target": 2, "stop": 1, "time_exit": 1}
    assert split_days(days, 0.25) == (days[:3], days[3:])
    checks = criteria(m, [{"expectancy_r": 0.1}])
    assert [c.passed for c in checks] == [False, True, True, True, True]  # only 4 trades


def test_metrics_with_no_trades():
    m = metrics(pd.DataFrame(), [date(2026, 9, 24)], 100_000)
    assert m["trades"] == 0 and m["max_dd"] == 0
