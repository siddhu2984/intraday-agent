"""Position sizing (architecture.md §4.7).

    risk_qty = floor(equity × risk_per_trade_pct / |entry − stop|)
    cap_qty  = floor(equity × max_position_pct × leverage / entry)
    qty      = min(risk_qty, cap_qty)

With tight intraday stops the position cap is often the smaller one, so the trade risks less than
risk_per_trade_pct. `binding` and `risk_amount` record which limit decided and what is actually at risk.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from agent.config import RiskConfig


def _floor(x: float) -> int:
    """Whole shares; rounds first so float error (500 / 0.1 = 4999.9999…) doesn't lose a share."""
    return math.floor(round(x, 6))


@dataclass(frozen=True)
class SizeResult:
    qty: int
    risk_qty: int
    cap_qty: int
    binding: str         # 'risk' | 'position_cap' | 'invalid'
    risk_amount: float   # qty × |entry − stop|: rupees lost if the stop is hit (before costs and slippage)


def size_position(entry: float, stop: float, equity: float, risk: RiskConfig) -> SizeResult:
    distance = abs(entry - stop)
    if entry <= 0 or distance == 0 or equity <= 0:
        return SizeResult(0, 0, 0, "invalid", 0.0)
    risk_qty = _floor(equity * risk.risk_per_trade_pct / 100 / distance)
    cap_qty = _floor(equity * risk.max_position_pct / 100 * risk.leverage / entry)
    qty = min(risk_qty, cap_qty)
    return SizeResult(qty, risk_qty, cap_qty, "risk" if risk_qty <= cap_qty else "position_cap", qty * distance)
