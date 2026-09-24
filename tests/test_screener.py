from datetime import date

import numpy as np
import pandas as pd
import pytest

from agent.data.download import INDEX_SYMBOL
from agent.strategy.screener import index_change_pct, stage_a, stage_b
from conftest import make_settings

SETTINGS = make_settings(screener={"stage_a_top_n": 3, "watchlist_size": 2})
DAY = date(2026, 9, 24)


def row(symbol, **kw):
    base = dict(day=DAY, symbol=symbol, member=True, open=102.0, prev_close=100.0, gap_pct=2.0, turnover_cr=50.0,
                atr_pct=2.0, series="EQ", band_pct=20.0, or_high=103.0, or_low=101.0, or_close=102.5,
                or_volume=3000, or_volume_avg=1000.0, or_bars=15, results=False, ex_date=False, sector="IT")
    base.update(kw)
    return base


def day_frame(*rows, index_change=0.5):
    index = row(INDEX_SYMBOL, member=False, prev_close=20_000.0, or_close=20_000.0 * (1 + index_change / 100),
                sector=None)
    return pd.DataFrame([*rows, index])


@pytest.mark.parametrize("change, reason", [
    ({"open": np.nan}, "no price data"),
    ({"series": None}, "not in NSE's security list"),
    ({"series": "BE"}, "trade-for-trade"),
    ({"prev_close": 49.9}, "price < 50"),
    ({"turnover_cr": np.nan}, "turnover history"),
    ({"turnover_cr": 19.9}, "turnover < 20"),
    ({"band_pct": 5.0}, "price band <= 5%"),
])
def test_stage_a_filters(change, reason):
    a = stage_a(day_frame(row("X", **change)), SETTINGS)
    assert not a.loc[0, "passed"] and reason in a.loc[0, "reason"]


def test_stage_a_passes_no_band_and_excludes_non_members_and_the_index():
    a = stage_a(day_frame(row("F&O", band_pct=np.nan), row("GONE", member=False)), SETTINGS)
    assert list(a["symbol"]) == ["F&O"] and a.loc[0, "passed"]


def test_stage_a_ranks_by_absolute_gap_and_keeps_top_n():
    a = stage_a(day_frame(row("A", gap_pct=1.0), row("B", gap_pct=-4.0), row("C", gap_pct=3.0),
                          row("D", gap_pct=2.0), row("E", gap_pct=0.5, prev_close=10)), SETTINGS)
    assert list(a.loc[a["passed"], "symbol"]) == ["B", "C", "D"]
    assert a.set_index("symbol").loc["A", "reason"] == "gap outside top 3"
    assert "price" in a.set_index("symbol").loc["E", "reason"]  # filtered, so not ranked


def test_index_change():
    assert index_change_pct(day_frame(index_change=0.5)) == pytest.approx(0.5)


@pytest.mark.parametrize("change, reason", [
    ({"or_bars": 0}, "no opening-range bars"),
    ({"or_volume_avg": np.nan}, "opening volume history"),
    ({"or_volume": 1999}, "RVOL < 2"),
    ({"atr_pct": np.nan}, "no ATR"),
    ({"atr_pct": 0.9}, "ATR < 1%"),
    ({"atr_pct": 5.1}, "ATR > 5%"),
    ({"or_close": 100.5}, "RS is 0"),     # +0.5% = the index's +0.5%
])
def test_stage_b_filters(change, reason):
    b = stage_b(pd.DataFrame([row("X", **change)]), 0.5, SETTINGS)
    assert not b.loc[0, "passed"] and reason in b.loc[0, "reason"]


def test_stage_b_rvol_at_exactly_the_minimum_passes():
    assert stage_b(pd.DataFrame([row("X", or_volume=2000)]), 0.5, SETTINGS).loc[0, "passed"]


def test_stage_b_direction_ranking_and_ties():
    b = stage_b(pd.DataFrame([
        row("UP", or_close=102.0, or_volume=3000),     # RS +1.5 → long
        row("DOWN", or_close=98.0, or_volume=5000),    # RS −2.5 → short, highest RVOL
        row("TIE", or_close=103.0, or_volume=3000),    # same RVOL as UP, larger |RS| → ranks first
    ]), 0.5, SETTINGS).set_index("symbol")
    assert b.loc["DOWN", "side"] == "short" and b.loc["UP", "side"] == "long"
    assert b.loc["DOWN", "rank"] == 1 and b.loc["TIE", "rank"] == 2
    assert not b.loc["UP", "passed"] and b.loc["UP", "reason"] == "RVOL outside top 2"
