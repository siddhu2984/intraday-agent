"""Candle indicators (architecture.md §4.2): 5-min candles, session VWAP, ATR, opening range.

Pure functions on one symbol's 1-min bars (columns ts, open, high, low, close, volume), shared by the backtest and,
from phase 3, the live market-data service.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

OHLCV = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def resample(bars: pd.DataFrame, minutes: int = 5) -> pd.DataFrame:
    """1-min bars → N-min candles, labelled by their start time (09:15, 09:20, …); empty buckets are dropped.

    A candle starting at t is complete at t + N minutes.
    """
    if bars.empty:
        return bars[["ts", *OHLCV]].copy()
    out = bars.set_index("ts")[list(OHLCV)].resample(f"{minutes}min", label="left", closed="left").agg(OHLCV)
    return out.dropna(subset=["open"]).reset_index()


def session_vwap(bars: pd.DataFrame) -> pd.Series:
    """VWAP after each 1-min bar, from the typical price (h + l + c) / 3; NaN while volume is 0 (e.g. an index).

    `bars` must be one session.
    """
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    volume = bars["volume"].astype("float64")
    cum_volume = volume.cumsum()
    return ((typical * volume).cumsum() / cum_volume.replace(0, np.nan)).rename("vwap")


def wilder_atr(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR after each candle: the first value is the mean of the first `period` true ranges, then
    ATR = (ATR_prev × (period − 1) + TR) / period. NaN before `period` candles."""
    prev_close = candles["close"].shift(1)
    tr = pd.concat([candles["high"] - candles["low"], (candles["high"] - prev_close).abs(),
                    (candles["low"] - prev_close).abs()], axis=1).max(axis=1).to_numpy()
    atr = np.full(len(tr), np.nan)
    if len(tr) >= period:
        atr[period - 1] = tr[:period].mean()
        for i in range(period, len(tr)):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return pd.Series(atr, index=candles.index, name="atr")


def opening_range(bars: pd.DataFrame, session_open: datetime, minutes: int) -> tuple[float, float] | None:
    """(high, low) of the bars in [open, open + minutes); None if there are none."""
    window = bars[(bars["ts"] >= session_open) & (bars["ts"] < session_open + timedelta(minutes=minutes))]
    if window.empty:
        return None
    return float(window["high"].max()), float(window["low"].min())
