"""Screener (architecture.md §4.4): Nifty 500 → Stage A candidates → Stage B watchlist.

Both stages work on one day's rows of the features table (agent.backtest.features), which holds only what is
known by 09:30. Every symbol gets a result row with the reason it failed, for the journal's screen_results.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from agent.config import Settings
from agent.data.download import INDEX_SYMBOL
from agent.risk.state import Side


def _first_failure(checks: list[tuple[pd.Series, str]], index) -> pd.Series:
    """Per row, the message of the first check that fails ('' if all pass)."""
    reason = pd.Series("", index=index, dtype=object)
    for ok, message in checks:
        reason = reason.where((reason != "") | ok.fillna(False).astype(bool), message)
    return reason


def stage_a(day: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Hard filters, then the largest |gap| first. Returns every member with passed / reason / rank."""
    sc = settings.screener
    df = day[day["member"] & (day["symbol"] != INDEX_SYMBOL)].copy()
    df["reason"] = _first_failure([
        (df["open"].notna() & df["prev_close"].notna(), "no price data today"),
        (df["series"].notna(), "not in NSE's security list"),
        (df["series"] == "EQ", "trade-for-trade series"),
        (df["prev_close"] >= sc.min_price, f"price < {sc.min_price:g}"),
        (df["turnover_cr"].notna(), "under 15 sessions of turnover history"),
        (df["turnover_cr"] >= sc.min_turnover_cr, f"avg turnover < {sc.min_turnover_cr:g} cr"),
        (df["band_pct"].isna() | (df["band_pct"] > sc.min_band_pct), f"price band <= {sc.min_band_pct:g}%"),
    ], df.index)
    ok = df["reason"] == ""
    df["abs_gap"] = df["gap_pct"].abs()
    df = df.sort_values(["abs_gap", "symbol"], ascending=[False, True])
    df["rank"] = np.nan
    df.loc[ok, "rank"] = np.arange(1, ok.sum() + 1)
    df.loc[ok & (df["rank"] > sc.stage_a_top_n), "reason"] = f"gap outside top {sc.stage_a_top_n}"
    df["passed"] = df["reason"] == ""
    return df.drop(columns="abs_gap").reset_index(drop=True)


def index_change_pct(day: pd.DataFrame) -> float:
    """NIFTY 50 % change from the previous close to the 09:29 close."""
    idx = day[day["symbol"] == INDEX_SYMBOL]
    if idx.empty or pd.isna(idx["or_close"].iloc[0]):
        return float("nan")
    return float((idx["or_close"].iloc[0] / idx["prev_close"].iloc[0] - 1) * 100)


def stage_b(candidates: pd.DataFrame, index_change: float, settings: Settings) -> pd.DataFrame:
    """RVOL, ATR% and relative strength at 09:30 on Stage A's candidates. Top by RVOL (ties: |RS|) → watchlist.

    Adds rvol, rs (percentage points vs NIFTY 50) and side (long if RS > 0, short if < 0).
    """
    sc = settings.screener
    df = candidates.copy()
    df["rvol"] = df["or_volume"] / df["or_volume_avg"]
    df["rs"] = (df["or_close"] / df["prev_close"] - 1) * 100 - index_change
    df["reason"] = _first_failure([
        (df["or_bars"].fillna(0) > 0, "no opening-range bars"),
        (pd.Series(not np.isnan(index_change), index=df.index), "no NIFTY 50 data"),
        (df["or_volume_avg"].notna() & (df["or_volume_avg"] > 0), "under 15 sessions of opening volume history"),
        (df["rvol"] >= sc.min_rvol, f"RVOL < {sc.min_rvol:g}"),
        (df["atr_pct"].notna(), "no ATR history"),
        (df["atr_pct"] >= sc.min_atr_pct, f"ATR < {sc.min_atr_pct:g}%"),
        (df["atr_pct"] <= sc.max_atr_pct, f"ATR > {sc.max_atr_pct:g}%"),
        (df["rs"].abs() > 1e-9, "RS is 0: no direction"),
    ], df.index)
    ok = df["reason"] == ""
    df["abs_rs"] = df["rs"].abs()
    df = df.sort_values(["rvol", "abs_rs", "symbol"], ascending=[False, False, True], na_position="last")
    df["rank"] = np.nan
    df.loc[ok, "rank"] = np.arange(1, ok.sum() + 1)
    df.loc[ok & (df["rank"] > sc.watchlist_size), "reason"] = f"RVOL outside top {sc.watchlist_size}"
    df["passed"] = df["reason"] == ""
    df["side"] = np.where(df["rs"] > 0, Side.LONG.value, Side.SHORT.value)
    return df.drop(columns="abs_rs").reset_index(drop=True)
