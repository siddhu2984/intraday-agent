"""Cleaning and quality checks for 1-min candles (architecture.md §4.2b)."""

from __future__ import annotations

from datetime import time

import pandas as pd

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 29)  # start of the last 1-min candle
SESSION_BARS = 375
# Σ 1-min volume ÷ daily volume. The daily bar also counts the pre-open auction, so a little below 1 is normal.
VOLUME_RATIO_RANGE = (0.90, 1.01)
MAX_OHLC_GAP = 0.005  # open/close beyond high/low by more than this fraction of price → bar is wrong


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only regular-session bars (drops the pre-open bars FYERS returns on some days) and drop duplicates."""
    times = df["ts"].dt.time
    df = df[(times >= SESSION_OPEN) & (times <= SESSION_CLOSE)]
    return df.drop_duplicates(subset=["symbol", "ts"], keep="last").sort_values("ts", ignore_index=True)


def check(bars: pd.DataFrame, expected_bars: int, daily_volume: float | None) -> tuple[str, list[str]]:
    """One symbol's bars for one day → ('ok' | 'warn' | 'fail', issues)."""
    failures, warnings = [], []

    prices = bars[["open", "high", "low", "close"]]
    if prices.isna().any().any() or (prices <= 0).any().any():
        failures.append("missing or non-positive price")
    # FYERS bars sometimes have open/close slightly outside high/low (mostly Nov 2023–Feb 2024, and the 09:15
    # bar's open = auction price). Small gaps are flagged; large ones mean the bar is wrong.
    body_low = bars[["open", "close"]].min(axis=1)
    body_high = bars[["open", "close"]].max(axis=1)
    outside = pd.concat([bars["low"] - body_low, body_high - bars["high"]], axis=1).max(axis=1) / bars["close"]
    if (outside > MAX_OHLC_GAP).any():
        failures.append(f"high/low inconsistent with open/close (max {outside.max():.2%})")
    elif (outside > 0).any():
        warnings.append(f"{int((outside > 0).sum())} bars with open/close outside high/low (max {outside.max():.2%})")
    if (bars["volume"] < 0).any():
        failures.append("negative volume")

    if len(bars) < expected_bars:
        warnings.append(f"{len(bars)}/{expected_bars} bars")
    if daily_volume:
        ratio = bars["volume"].sum() / daily_volume
        if not VOLUME_RATIO_RANGE[0] <= ratio <= VOLUME_RATIO_RANGE[1]:
            warnings.append(f"1-min volume is {ratio:.3f}x daily")

    status = "fail" if failures else "warn" if warnings else "ok"
    return status, failures + warnings
