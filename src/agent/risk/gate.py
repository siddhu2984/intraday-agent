"""Risk gate (architecture.md §4.7): every order intent passes here, entries and exits.

check_entry runs every check — not just up to the first failure — so the journal shows all the reasons a trade
was blocked. Unknown inputs (sector, circuit limits) are rejections: the gate fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agent.broker.fyers_auth import IST
from agent.config import Settings
from agent.ops.calendar import NseCalendar
from agent.risk.sizing import SizeResult, size_position
from agent.risk.state import EntryIntent, ExitIntent, RiskState, Side

EPS = 1e-9  # tolerance for limits compared exactly at the boundary (e.g. a stop exactly min_stop_pct away)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Decision:
    approved: bool
    qty: int
    reason: str                       # 'approved', or the first failed check's detail
    checks: tuple[Check, ...] = ()
    sizing: SizeResult | None = None

    @property
    def failed(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]


def _ist(now: datetime) -> datetime:
    return now.astimezone(IST) if now.tzinfo else now


def breach(state: RiskState, settings: Settings) -> str | None:
    """A limit that should engage the kill switch for the rest of the day, or None."""
    r = settings.risk
    limit = state.start_equity * r.max_daily_loss_pct / 100
    if state.daily_pnl <= -limit:
        return f"daily loss {state.daily_pnl:,.0f} reached the limit of -{limit:,.0f} ({r.max_daily_loss_pct}%)"
    if state.consecutive_losses >= r.max_consecutive_losses:
        return f"{state.consecutive_losses} consecutive losses (limit {r.max_consecutive_losses})"
    return None


def check_entry(intent: EntryIntent, state: RiskState, settings: Settings, calendar: NseCalendar) -> Decision:
    r, st = settings.risk, settings.strategy
    checks: list[Check] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append(Check(name, bool(passed), detail))

    check("kill_switch", not state.kill_switch, "kill switch engaged")
    check("data_healthy", state.data_healthy, "market data stale")
    check("reconcile_ok", state.reconcile_ok, "reconcile mismatch unresolved")

    now = _ist(state.now)
    window = calendar.entry_window(now.date(), st)
    if window is None:
        check("entry_window", False, f"{now.date()} is not a trading day")
    else:
        first, last = window
        check("entry_window", first <= now.time() < last,
              f"{now:%H:%M:%S} outside the entry window {first:%H:%M}-{last:%H:%M}")

    loss_limit = state.start_equity * r.max_daily_loss_pct / 100
    check("daily_loss", state.daily_pnl > -loss_limit,
          f"daily P&L {state.daily_pnl:,.0f} at or below -{loss_limit:,.0f}")
    check("consecutive_losses", state.consecutive_losses < r.max_consecutive_losses,
          f"{state.consecutive_losses} consecutive losses (max {r.max_consecutive_losses})")
    check("trades_per_day", state.trades_today < r.max_trades_per_day,
          f"{state.trades_today} trades today (max {r.max_trades_per_day})")
    check("open_positions", len(state.positions) < r.max_open_positions,
          f"{len(state.positions)} open positions (max {r.max_open_positions})")
    check("symbol_not_held", all(p.symbol != intent.symbol for p in state.positions),
          f"already holding {intent.symbol}")

    if intent.sector is None:
        check("sector", False, f"sector unknown for {intent.symbol}")
    else:
        same = sum(p.sector == intent.sector for p in state.positions)
        check("sector", same < r.max_per_sector, f"{same} open positions in {intent.sector} (max {r.max_per_sector})")

    right_side = intent.stop < intent.entry if intent.side is Side.LONG else intent.stop > intent.entry
    stop_pct = abs(intent.entry - intent.stop) / intent.entry * 100 if intent.entry > 0 else 0.0
    if intent.entry <= 0 or not right_side:
        check("stop", False, f"stop {intent.stop} on the wrong side of entry {intent.entry} for a {intent.side.value}")
    else:
        check("stop", stop_pct + EPS >= st.min_stop_pct,
              f"stop distance {stop_pct:.2f}% below min_stop_pct {st.min_stop_pct}%")

    sizing = None
    if checks[-1].passed:
        sizing = size_position(intent.entry, intent.stop, state.start_equity, r)
    qty = sizing.qty if sizing else 0
    check("quantity", qty >= 1, "position size rounds to 0 shares" if sizing else "not sized (invalid stop)")

    margin = qty * intent.entry / r.leverage
    allowed = state.available_funds * r.margin_buffer
    check("margin", margin <= allowed + EPS, f"margin {margin:,.0f} exceeds {allowed:,.0f} "
          f"({r.margin_buffer:.0%} of available funds)")

    if intent.upper_circuit is None or intent.lower_circuit is None:
        check("circuit", False, f"circuit limits unknown for {intent.symbol}")
    else:
        to_upper = (intent.upper_circuit - intent.entry) / intent.entry * 100
        to_lower = (intent.entry - intent.lower_circuit) / intent.entry * 100
        check("circuit", min(to_upper, to_lower) >= r.circuit_buffer_pct - EPS,
              f"entry within {r.circuit_buffer_pct}% of a circuit limit "
              f"({to_upper:.2f}% to upper, {to_lower:.2f}% to lower)")

    failed = [c for c in checks if not c.passed]
    return Decision(not failed, qty if not failed else 0, failed[0].detail if failed else "approved",
                    tuple(checks), sizing)


def check_exit(intent: ExitIntent, state: RiskState) -> Decision:
    """An exit only ever reduces a held position; allowed with the kill switch on or the entry window closed."""
    held = next((p for p in state.positions if p.symbol == intent.symbol), None)
    if held is None:
        reason = f"no open position in {intent.symbol}"
    elif held.side is not intent.side:
        reason = f"position in {intent.symbol} is {held.side.value}, exit is for a {intent.side.value}"
    elif not 1 <= intent.qty <= held.qty:
        reason = f"exit qty {intent.qty} not in 1..{held.qty} held"
    else:
        return Decision(True, intent.qty, "approved", (Check("reduces_position", True, ""),))
    return Decision(False, 0, reason, (Check("reduces_position", False, reason),))
