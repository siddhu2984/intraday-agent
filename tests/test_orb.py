import json
from datetime import date, datetime

import pandas as pd
import pytest

from agent.broker.fyers_auth import IST
from agent.config import ConfigError, apply_overrides, load_settings
from agent.journal.db import Journal
from agent.risk.state import Side
from agent.strategy.orb import SymbolDay, orb_signal
from conftest import make_settings, settings_dict

ST = make_settings().strategy
END = pd.Timestamp(datetime(2026, 9, 24, 10, 0, tzinfo=IST))


def symbol_day(side=Side.LONG, **candle):
    c = dict(ts=END - pd.Timedelta(minutes=5), end=END, open=101.0, high=102.0, low=100.8, close=101.8,
             volume=1000, vwap=101.0, atr=1.0, index=20_000.0)
    c.update(candle)
    return SymbolDay("NSE:AAA-EQ", side, prev_close=100.0, index_prev_close=20_000.0, or_high=101.5,
                     or_low=99.5, candles=pd.DataFrame([c]), by_end={END: 0})


def signal(sd):
    return orb_signal(sd, sd.candles.iloc[0], ST)


def test_long_breakout():
    s = signal(symbol_day())
    assert s.side is Side.LONG and s.ts == END.to_pydatetime()
    assert s.entry == pytest.approx(101.8 * 1.0005)
    assert s.stop == 100.8                                     # candle low, inside the 1.5 × ATR cap
    assert s.target == pytest.approx(s.entry + 2 * (s.entry - 100.8))


@pytest.mark.parametrize("change", [
    {"close": 101.5},              # not above the OR high (must close beyond it)
    {"vwap": 101.9},               # below VWAP
    {"index": 20_400.0},           # index +2% > stock +1.8% → RS < 0
    {"atr": float("nan")},         # no ATR yet
])
def test_long_needs_every_condition(change):
    assert signal(symbol_day(**change)) is None


def test_side_must_match_stage_b_direction():
    assert signal(symbol_day(side=Side.SHORT)) is None


def test_stop_is_capped_by_atr():
    s = signal(symbol_day(low=99.0, atr=0.5))                 # candle low 2.8 away, cap 0.75
    assert s.stop == pytest.approx(s.entry - 0.75)


def test_too_tight_stop_is_skipped():
    assert signal(symbol_day(low=101.6, atr=1.0)) is None     # 0.25% < min_stop_pct 0.3%


def test_short_breakdown():
    s = signal(symbol_day(side=Side.SHORT, open=99.6, high=99.9, low=99.0, close=99.1, vwap=99.8))
    assert s.side is Side.SHORT and s.stop == 99.9 and s.target < s.entry


# --- config overrides, journal bulk writers ---

def test_overrides():
    data = apply_overrides(settings_dict(), ["risk.leverage=5", "backtest.block_corporate_events=false"])
    assert data["risk"]["leverage"] == 5 and data["backtest"]["block_corporate_events"] is False
    for bad in ("risk.levrage=5", "nosection.x=1", "risk.leverage"):
        with pytest.raises(ConfigError):
            apply_overrides(settings_dict(), [bad])


def test_load_settings_with_override():
    assert load_settings(overrides=["risk.leverage=2"]).risk.leverage == 2.0


def test_journal_bulk_writers():
    j = Journal(":memory:")
    j.start_run(make_settings(), run_id="test-run")
    ts = datetime(2026, 9, 24, 9, 30, tzinfo=IST)
    j.log_screen_results([{"day": date(2026, 9, 24), "ts": ts, "symbol": "A", "stage": "B", "passed": True,
                           "reason": "", "detail": {"rvol": 2.5}}])
    j.log_signals([{"ts": ts, "symbol": "A", "side": "long", "entry": 1.0, "stop": 0.9, "target": 1.2,
                    "reason": "r", "outcome": "blocked", "detail": "hard block", "features": {}}])
    j.log_trades([{"symbol": "A", "side": "long", "qty": 1, "entry_ts": ts, "entry_price": 1.0, "exit_ts": ts,
                   "exit_price": 1.2, "exit_reason": "target", "r_multiple": 2.0, "pnl_gross": 0.2, "costs": 0.0,
                   "pnl_net": 0.2, "day": date(2026, 9, 24), "stop": 0.9}])
    assert j.run_id == "test-run"
    assert json.loads(j.rows("screen_results")[0]["detail"]) == {"reason": "", "rvol": 2.5}
    assert json.loads(j.rows("signals")[0]["detail"])["outcome"] == "blocked"
    [trade] = j.rows("trades")
    assert trade["entry_ts"] == ts.isoformat() and json.loads(trade["detail"]) == {"day": "2026-09-24", "stop": 0.9}
