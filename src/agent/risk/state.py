"""What the risk gate decides on (architecture.md §4.7): order intents and a snapshot of the account and day.

The gate itself is stateless. Whoever runs the day — the backtester, later the OMS — keeps a DayCounters and builds
a RiskState from it (plus broker funds, positions and health flags) for every decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    sector: str | None
    side: Side
    qty: int
    entry: float
    stop: float


@dataclass(frozen=True)
class EntryIntent:
    symbol: str
    sector: str | None       # NSE industry from nifty500_members.csv; None = unknown → rejected
    side: Side
    entry: float
    stop: float
    upper_circuit: float | None  # today's price band; None = unknown → rejected
    lower_circuit: float | None


@dataclass(frozen=True)
class ExitIntent:
    symbol: str
    side: Side               # side of the position being reduced, not the order direction
    qty: int


@dataclass(frozen=True)
class RiskState:
    now: datetime            # decision time, IST (simulated time in a backtest)
    start_equity: float      # equity at the start of the day; all % limits and sizing use it
    available_funds: float   # margin available for new positions (broker funds in live)
    realized_pnl: float      # today, net of costs
    unrealized_pnl: float
    consecutive_losses: int
    trades_today: int        # entries today, including open ones
    positions: tuple[OpenPosition, ...] = ()
    kill_switch: bool = False
    data_healthy: bool = True
    reconcile_ok: bool = True

    @property
    def daily_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl


@dataclass
class DayCounters:
    """Per-day counters the gate's limits apply to. A new day starts a new DayCounters."""

    day: date
    trades: int = 0
    consecutive_losses: int = 0
    realized_pnl: float = 0.0

    def record_entry(self) -> None:
        self.trades += 1

    def record_close(self, pnl_net: float) -> None:
        """A trade closed. A loss (net P&L < 0, costs included) extends the streak; anything else resets it."""
        self.realized_pnl += pnl_net
        self.consecutive_losses = self.consecutive_losses + 1 if pnl_net < 0 else 0
