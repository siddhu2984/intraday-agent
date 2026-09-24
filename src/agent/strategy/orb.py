"""ORB v1 (architecture.md §4.5) and the random-entry baseline (§9).

A watchlist stock's day is prepared once (SymbolDay: 5-min candles with VWAP, ATR and the index level at each
candle's close); the strategy then looks at each completed 5-min candle.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from agent.config import StrategyConfig
from agent.data.indicators import resample, session_vwap, wilder_atr
from agent.risk.state import Side

ATR_PERIOD = 14


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    ts: datetime          # decision time = the breakout candle's close
    entry: float          # limit price
    stop: float
    target: float
    reason: str
    features: dict = field(default_factory=dict)


@dataclass
class SymbolDay:
    symbol: str
    side: Side                     # direction allowed today (Stage B relative strength)
    prev_close: float
    index_prev_close: float
    or_high: float
    or_low: float
    candles: pd.DataFrame          # today's 5-min candles: ts, end, open, high, low, close, volume, vwap, atr, index
    by_end: dict = field(default_factory=dict)  # candle close time → row position

    def candle_ending(self, ts: datetime) -> pd.Series | None:
        i = self.by_end.get(ts)
        return None if i is None else self.candles.iloc[i]


def prepare_symbol_day(symbol: str, side: Side, bars: pd.DataFrame, prev_bars: pd.DataFrame,
                       index_bars: pd.DataFrame, prev_close: float, index_prev_close: float,
                       or_high: float, or_low: float) -> SymbolDay:
    """`bars`/`index_bars`: today's 1-min bars; `prev_bars`: the previous session's, to warm up the 5-min ATR."""
    both = resample(pd.concat([prev_bars, bars], ignore_index=True)) if not prev_bars.empty else resample(bars)
    both["atr"] = wilder_atr(both, ATR_PERIOD)
    day = bars["ts"].iloc[0].date()
    candles = both[both["ts"].dt.date == day].reset_index(drop=True)
    candles["end"] = candles["ts"] + timedelta(minutes=5)
    last_minute = candles["end"] - timedelta(minutes=1)

    vwap = pd.DataFrame({"ts": bars["ts"], "vwap": session_vwap(bars.reset_index(drop=True)).to_numpy()})
    index = index_bars[["ts", "close"]].rename(columns={"close": "index"})
    index["ts"] = index["ts"].astype(bars["ts"].dtype)
    probe = pd.DataFrame({"ts": last_minute.astype(bars["ts"].dtype)})  # resample may change the time unit
    candles["vwap"] = pd.merge_asof(probe, vwap, on="ts")["vwap"].to_numpy()
    candles["index"] = pd.merge_asof(probe, index.sort_values("ts"), on="ts")["index"].to_numpy()
    return SymbolDay(symbol, side, prev_close, index_prev_close, or_high, or_low, candles,
                     {ts: i for i, ts in enumerate(candles["end"])})


def relative_strength(sd: SymbolDay, candle: pd.Series) -> float:
    """Stock % change minus NIFTY 50 % change, both from the previous close to this candle's close."""
    return ((candle["close"] / sd.prev_close) - (candle["index"] / sd.index_prev_close)) * 100


def build_signal(sd: SymbolDay, candle: pd.Series, side: Side, st: StrategyConfig, reason: str) -> Signal | None:
    """Entry at the close ± buffer; stop beyond the candle, capped at max_stop_atr × ATR; None if too tight."""
    atr = candle["atr"]
    if pd.isna(atr):
        return None
    buffer = st.entry_buffer_pct / 100
    if side is Side.LONG:
        entry = candle["close"] * (1 + buffer)
        stop = max(candle["low"], entry - st.max_stop_atr * atr)
    else:
        entry = candle["close"] * (1 - buffer)
        stop = min(candle["high"], entry + st.max_stop_atr * atr)
    risk = abs(entry - stop)
    if risk / entry * 100 < st.min_stop_pct:
        return None
    target = entry + st.target_r * risk if side is Side.LONG else entry - st.target_r * risk
    features = {"close": float(candle["close"]), "vwap": float(candle["vwap"]), "atr": float(atr),
                "or_high": sd.or_high, "or_low": sd.or_low, "rs": float(relative_strength(sd, candle))}
    return Signal(sd.symbol, side, candle["end"].to_pydatetime(), float(entry), float(stop), float(target),
                  reason, features)


def orb_signal(sd: SymbolDay, candle: pd.Series, st: StrategyConfig) -> Signal | None:
    """§4.5: a 5-min candle closes beyond the opening range, on the right side of VWAP, with RS agreeing."""
    rs = relative_strength(sd, candle)
    if sd.side is Side.LONG and candle["close"] > sd.or_high and candle["close"] > candle["vwap"] and rs > 0:
        return build_signal(sd, candle, Side.LONG, st, "close above OR high and VWAP, RS > 0")
    if sd.side is Side.SHORT and candle["close"] < sd.or_low and candle["close"] < candle["vwap"] and rs < 0:
        return build_signal(sd, candle, Side.SHORT, st, "close below OR low and VWAP, RS < 0")
    return None


class RandomEntry:
    """Baseline: same watchlist, stop rule, exits and risk gate — but entry time and side are random.

    Each watchlist stock trades with `probability`: a random side, at a random candle close in the entry window
    among those where the stop rule gives a valid stop (a random 5-min candle is often narrower than
    min_stop_pct, and skipping those would make the baseline trade far less than ORB). Seeded per
    (seed, day, symbol), so results don't depend on the order days are run in.
    """

    def __init__(self, seed: int, probability: float):
        self.seed, self.probability = seed, probability
        self._plans: dict[tuple[date, str], tuple[datetime, Side] | None] = {}

    def plan(self, sd: SymbolDay, st: StrategyConfig, eligible_ends: list[datetime]) -> tuple[datetime, Side] | None:
        key = (sd.candles["ts"].iloc[0].date(), sd.symbol)
        if key not in self._plans:
            rng = np.random.default_rng([self.seed, key[0].toordinal(), zlib.crc32(sd.symbol.encode())])
            side = Side.LONG if rng.random() < 0.5 else Side.SHORT
            valid = [e for e in eligible_ends if (c := sd.candle_ending(e)) is not None
                     and build_signal(sd, c, side, st, "") is not None]
            trade = rng.random() < self.probability
            self._plans[key] = (valid[rng.integers(len(valid))], side) if trade and valid else None
        return self._plans[key]

    def signal(self, sd: SymbolDay, candle: pd.Series, st: StrategyConfig,
               eligible_ends: list[datetime]) -> Signal | None:
        plan = self.plan(sd, st, eligible_ends)
        if plan is None or plan[0] != candle["end"]:
            return None
        return build_signal(sd, candle, plan[1], st, f"random entry (seed {self.seed})")
