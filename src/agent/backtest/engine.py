"""Backtest engine (architecture.md §9): replays days through screener → strategy → hard blocks → risk gate →
SimBroker, with a small trade manager standing in for the phase 3 OMS.

Each day starts with the configured capital (no compounding), so days are independent and run in parallel.

Per 1-min step t (09:30 … 15:29):
  1. at a 5-min boundary: the candle that just closed is checked for signals; approved ones place an entry
     limit valid for one bar (the 60 s timeout)
  2. at force_exit (15:10): cancel everything, market-exit open positions
  3. every symbol with orders is matched against its bar t; an entry fill immediately gets a broker-side stop
     (SL-M) and a target limit; one exit filling cancels the other
  4. after the bar: move the stop to breakeven once the bar reached +breakeven_at_r
A loss limit breach engages the kill switch: no new entries that day, exits continue.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd

from agent.backtest.costs import round_trip
from agent.broker.base import Action, Fill, OrderRequest, OrderType
from agent.broker.fyers_auth import IST
from agent.broker.sim import Bar, SimBroker
from agent.config import Settings
from agent.data.download import INDEX_SYMBOL
from agent.data.store import CandleStore
from agent.ops.calendar import NseCalendar
from agent.ops.killswitch import KillSwitch
from agent.risk.gate import breach, check_entry
from agent.risk.state import DayCounters, EntryIntent, OpenPosition, RiskState, Side
from agent.strategy.orb import RandomEntry, Signal, SymbolDay, orb_signal, prepare_symbol_day
from agent.strategy.screener import index_change_pct, stage_a, stage_b


@dataclass
class Trade:
    day: date
    symbol: str
    sector: str | None
    side: Side
    qty: int
    signal_ts: datetime
    entry_limit: float
    stop: float                     # initial stop
    target: float
    binding: str                    # what decided the size: 'risk' | 'position_cap'
    entry_ts: datetime | None = None
    entry_price: float | None = None
    exit_ts: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None  # target | stop | breakeven | time_exit
    breakeven_moved: bool = False
    status: str = "pending"         # pending | open | closed | cancelled
    entry_order: str | None = None
    stop_order: str | None = None
    target_order: str | None = None
    exit_order: str | None = None
    pnl_gross: float = 0.0
    costs: float = 0.0
    pnl_net: float = 0.0
    risk_amount: float = 0.0        # qty × |entry fill − initial stop|
    r_multiple: float = 0.0

    @property
    def long(self) -> bool:
        return self.side is Side.LONG

    def as_row(self) -> dict:
        row = asdict(self)
        row["side"] = self.side.value
        return row


@dataclass
class DayResult:
    day: date
    trades: list[dict] = field(default_factory=list)
    signals: list[dict] = field(default_factory=list)      # every signal with what happened to it
    screen: list[dict] = field(default_factory=list)       # Stage A + B rows
    events: list[dict] = field(default_factory=list)       # kill switch etc.


class TradeManager:
    """Entry → protective stop + target → one exit cancels the other → breakeven → time exit."""

    def __init__(self, broker: SimBroker, counters: DayCounters, settings: Settings):
        self.broker, self.counters, self.settings = broker, counters, settings
        self.trades: list[Trade] = []
        self._by_order: dict[str, Trade] = {}

    def active(self) -> list[Trade]:
        return [t for t in self.trades if t.status in ("pending", "open")]

    def symbols_with_orders(self) -> list[str]:
        return sorted({t.symbol for t in self.active()})

    def open_entry(self, signal: Signal, qty: int, sector: str | None, binding: str) -> Trade:
        t = Trade(signal.ts.date(), signal.symbol, sector, signal.side, qty, signal.ts, signal.entry, signal.stop,
                  signal.target, binding)
        tag = f"{signal.symbol}-{signal.ts:%H%M}"
        action = Action.BUY if t.long else Action.SELL
        t.entry_order = self.broker.place(
            OrderRequest(f"{tag}-entry", t.symbol, action, qty, OrderType.LIMIT, price=signal.entry, valid_bars=1),
            signal.ts)
        self._by_order[t.entry_order] = t
        self.trades.append(t)
        return t

    def on_fill(self, fill: Fill) -> None:
        t = self._by_order[fill.order_id]
        exit_action = Action.SELL if t.long else Action.BUY
        if fill.order_id == t.entry_order:
            t.status, t.entry_ts, t.entry_price = "open", fill.ts, fill.price
            t.risk_amount = t.qty * abs(fill.price - t.stop)
            self.counters.record_entry()
            tag = t.entry_order
            t.stop_order = self.broker.place(OrderRequest(f"{tag}-stop", t.symbol, exit_action, t.qty,
                                                          OrderType.SL_M, trigger=t.stop), fill.ts)
            t.target_order = self.broker.place(OrderRequest(f"{tag}-target", t.symbol, exit_action, t.qty,
                                                            OrderType.LIMIT, price=t.target), fill.ts)
            self._by_order[t.stop_order] = self._by_order[t.target_order] = t
            return
        if fill.order_id == t.stop_order:
            reason = "breakeven" if t.breakeven_moved else "stop"
        elif fill.order_id == t.target_order:
            reason = "target"
        else:
            reason = "time_exit"
        for sibling in (t.stop_order, t.target_order):
            if sibling and sibling != fill.order_id:
                self.broker.cancel(sibling)
        self._close(t, fill, reason)

    def _close(self, t: Trade, fill: Fill, reason: str) -> None:
        t.status, t.exit_ts, t.exit_price, t.exit_reason = "closed", fill.ts, fill.price, reason
        sign = 1 if t.long else -1
        t.pnl_gross = sign * (fill.price - t.entry_price) * t.qty
        t.costs = round_trip(t.side, t.qty, t.entry_price, fill.price, self.settings.costs).total
        t.pnl_net = t.pnl_gross - t.costs
        t.r_multiple = t.pnl_net / t.risk_amount if t.risk_amount else 0.0
        self.counters.record_close(t.pnl_net)

    def after_bar(self, symbol: str, bar: Bar) -> None:
        """Expired entries become cancelled trades; the stop moves to breakeven once +breakeven_at_r is reached."""
        for t in self.trades:
            if t.symbol != symbol:
                continue
            if t.status == "pending" and self.broker.orders[t.entry_order].status.value == "cancelled":
                t.status = "cancelled"
            elif t.status == "open" and not t.breakeven_moved and t.entry_ts < bar.ts:
                r = abs(t.entry_price - t.stop)
                reach = self.settings.strategy.breakeven_at_r * r
                if (bar.high >= t.entry_price + reach) if t.long else (bar.low <= t.entry_price - reach):
                    self.broker.modify(t.stop_order, trigger=t.entry_price)
                    t.breakeven_moved = True

    def flatten(self, ts: datetime) -> None:
        """Time exit: cancel pending entries and resting exits; market-exit open positions."""
        for t in self.active():
            if t.status == "pending":
                self.broker.cancel(t.entry_order)
                t.status = "cancelled"
                continue
            for order_id in (t.stop_order, t.target_order):
                self.broker.cancel(order_id)
            action = Action.SELL if t.long else Action.BUY
            t.exit_order = self.broker.place(OrderRequest(f"{t.entry_order}-exit", t.symbol, action, t.qty,
                                                          OrderType.MARKET), ts)
            self._by_order[t.exit_order] = t

    def close_at(self, t: Trade, price: float, ts: datetime) -> None:
        """No bar left to exit on (data ends): close at the last price, as a time exit."""
        if t.exit_order:
            self.broker.cancel(t.exit_order)
        self._close(t, Fill("eod", "eod", t.symbol, Action.SELL if t.long else Action.BUY, t.qty, price, ts),
                    "time_exit")


def _at(day: date, t: time) -> datetime:
    return datetime.combine(day, t, IST)


def _circuits(prev_close: float, band_pct: float, settings: Settings) -> tuple[float, float]:
    band = settings.backtest.no_band_assumed_pct if pd.isna(band_pct) else band_pct
    return prev_close * (1 + band / 100), prev_close * (1 - band / 100)


def _bars_by_minute(bars: pd.DataFrame) -> dict[datetime, Bar]:
    return {ts.to_pydatetime(): Bar(ts.to_pydatetime(), o, h, l, c, v) for ts, o, h, l, c, v in
            zip(bars["ts"], bars["open"], bars["high"], bars["low"], bars["close"], bars["volume"])}


def run_day(day: date, features_day: pd.DataFrame, settings: Settings, calendar: NseCalendar,
            store: CandleStore, strategy: RandomEntry | None = None) -> DayResult:
    """One trading day. `strategy` None = ORB v1; a RandomEntry = the baseline."""
    result = DayResult(day)
    session = calendar.session(day)
    if session is None:
        return result
    st, bt = settings.strategy, settings.backtest

    # --- screener ---
    window = calendar.entry_window(day, st)
    stage_ts = {"A": _at(day, time(9, 8)), "B": _at(day, window[0])}
    a = stage_a(features_day, settings)
    b = stage_b(a[a["passed"]], index_change_pct(features_day), settings)
    for stage, frame, cols in (("A", a, ["gap_pct", "prev_close", "turnover_cr", "band_pct", "series"]),
                               ("B", b, ["rvol", "atr_pct", "rs", "side"])):
        for r in frame.itertuples(index=False):
            detail = {c: getattr(r, c) for c in cols + ["rank"]}
            result.screen.append({"day": day, "ts": stage_ts[stage], "symbol": r.symbol, "stage": stage,
                                  "passed": bool(r.passed),
                                  "reason": r.reason, "detail": detail})
    watch = b[b["passed"]]
    if watch.empty:
        return result

    # --- prepare the watchlist's candles ---
    symbols = list(watch["symbol"])
    today = store.read_day(day, symbols + [INDEX_SYMBOL])
    prev_day = calendar.previous_trading_day(day)
    prev = store.read_day(prev_day, symbols) if store.day_path(prev_day).exists() else today.iloc[0:0]
    index_bars = today[today["symbol"] == INDEX_SYMBOL]
    index_prev_close = float(features_day.loc[features_day["symbol"] == INDEX_SYMBOL, "prev_close"].iloc[0])
    days: dict[str, SymbolDay] = {}
    rows: dict[str, object] = {}
    minute_bars: dict[str, dict[datetime, Bar]] = {}
    for w in watch.itertuples(index=False):
        bars = today[today["symbol"] == w.symbol].reset_index(drop=True)
        if bars.empty or index_bars.empty:
            continue
        days[w.symbol] = prepare_symbol_day(
            w.symbol, Side(w.side), bars, prev[prev["symbol"] == w.symbol], index_bars, w.prev_close,
            index_prev_close, w.or_high, w.or_low)
        rows[w.symbol] = w
        minute_bars[w.symbol] = _bars_by_minute(bars)

    # --- the session ---
    capital = settings.capital
    broker = SimBroker(bt.slippage_bps)
    counters = DayCounters(day)
    tm = TradeManager(broker, counters, settings)
    kill = KillSwitch(None)
    traded: set[str] = set()
    first_decision = _at(day, window[0]) + timedelta(minutes=5)
    last_decision = _at(day, window[1])  # the gate would reject later signals anyway; skip computing them
    eligible_ends = [_at(day, window[0]) + timedelta(minutes=5 * k)
                     for k in range(1, 400) if (_at(day, window[0]) + timedelta(minutes=5 * k)).time() < window[1]]
    force_exit = _at(day, st.force_exit)
    close_dt = _at(day, session.close)
    last_price: dict[str, float] = {}

    def risk_state(now: datetime) -> RiskState:
        active = tm.active()
        positions = tuple(OpenPosition(t.symbol, t.sector, t.side, t.qty, t.entry_price or t.entry_limit, t.stop)
                          for t in active)
        unrealized = sum(((last_price.get(t.symbol, t.entry_price) - t.entry_price) * t.qty * (1 if t.long else -1))
                         for t in active if t.status == "open")
        margin_used = sum(t.qty * (t.entry_price or t.entry_limit) / settings.risk.leverage for t in active)
        pending = sum(t.status == "pending" for t in active)
        status = kill.status(now)
        return RiskState(now, capital, capital + counters.realized_pnl - margin_used, counters.realized_pnl,
                         unrealized, counters.consecutive_losses, counters.trades + pending, positions,
                         kill_switch=status.engaged)

    now = _at(day, window[0])
    while now < close_dt:
        # 1. signals on the candle that just closed
        if first_decision <= now < last_decision and (now - _at(day, session.open)).seconds % 300 == 0:
            for symbol, sd in days.items():
                if symbol in traded:
                    continue
                candle = sd.candle_ending(now)
                if candle is None:
                    continue
                signal = (strategy.signal(sd, candle, st, eligible_ends) if strategy
                          else orb_signal(sd, candle, st))
                if signal is None:
                    continue
                w = rows[symbol]
                record = {"day": day, "ts": now, "symbol": symbol, "side": signal.side.value,
                          "entry": signal.entry, "stop": signal.stop, "target": signal.target,
                          "reason": signal.reason, "features": signal.features}
                blocks = [k for k in ("results", "ex_date") if bt.block_corporate_events and bool(getattr(w, k))]
                if blocks:
                    record.update(outcome="blocked", detail=f"hard block: {', '.join(blocks)} today")
                    traded.add(symbol)
                    result.signals.append(record)
                    continue
                upper, lower = _circuits(w.prev_close, w.band_pct, settings)
                intent = EntryIntent(symbol, w.sector, signal.side, signal.entry, signal.stop, upper, lower)
                decision = check_entry(intent, risk_state(now), settings, calendar)
                record.update(outcome="approved" if decision.approved else "rejected", detail=decision.reason,
                              qty=decision.qty, intent=intent, decision=decision)
                result.signals.append(record)
                if decision.approved:
                    tm.open_entry(signal, decision.qty, w.sector, decision.sizing.binding)
                    traded.add(symbol)

        # 2. time exit
        if now == force_exit:
            tm.flatten(now)

        # 3–4. match orders, manage trades
        for symbol in tm.symbols_with_orders():
            bar = minute_bars[symbol].get(now)
            if bar is None:
                continue
            last_price[symbol] = bar.close
            broker.process_bar(symbol, bar, on_fill=tm.on_fill)
            tm.after_bar(symbol, bar)
        for symbol, bars in minute_bars.items():
            if now in bars:
                last_price[symbol] = bars[now].close

        reason = breach(risk_state(now), settings)
        if reason and not kill.status(now).engaged:
            kill.engage(reason, now)
            result.events.append({"day": day, "ts": now, "kind": "KILL_SWITCH", "message": reason})
        now += timedelta(minutes=1)

    for t in tm.active():  # data ended before the exit filled
        if t.status == "open":
            tm.close_at(t, last_price.get(t.symbol, t.entry_price), close_dt)
        else:
            t.status = "cancelled"
    result.trades = [t.as_row() for t in tm.trades if t.status == "closed"]
    return result


# --- running many days ---

def _run_days(days: list[date], features: pd.DataFrame, settings: Settings, seed: int | None,
              probability: float) -> list[DayResult]:
    calendar = NseCalendar.load()
    store = CandleStore()
    strategy = RandomEntry(seed, probability) if seed is not None else None
    try:
        by_day = dict(tuple(features.groupby("day")))
        return [run_day(d, by_day[d], settings, calendar, store, strategy) for d in days if d in by_day]
    finally:
        store.close()


def run(settings: Settings, features: pd.DataFrame, days: list[date], workers: int | None = None,
        seed: int | None = None, probability: float = 0.0) -> list[DayResult]:
    """All days, in parallel chunks of consecutive days. Results come back in day order."""
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    if workers == 1 or len(days) < 20:
        return _run_days(days, features, settings, seed, probability)
    size = max(5, len(days) // (workers * 4))
    chunks = [days[i:i + size] for i in range(0, len(days), size)]
    with ProcessPoolExecutor(workers) as pool:
        futures = [pool.submit(_run_days, c, features[features["day"].isin(set(c))], settings, seed, probability)
                   for c in chunks]
        return [r for f in futures for r in f.result()]
