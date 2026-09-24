"""Backtest report (architecture.md §9): metrics, walk-forward split, random baseline, pass/fail per criterion."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from agent.backtest.engine import DayResult
from agent.config import Settings

# §9 success criteria, checked on the validation period
MIN_TRADES = 100
MIN_EXPECTANCY_R = 0.15
MIN_PROFIT_FACTOR = 1.3
MAX_DRAWDOWN_PCT = 8.0


def trades_frame(results: list[DayResult]) -> pd.DataFrame:
    rows = [t for r in results for t in r.trades]
    return pd.DataFrame(rows).sort_values(["day", "entry_ts"], ignore_index=True) if rows else pd.DataFrame()


def split_days(days: list[date], validation_fraction: float) -> tuple[list[date], list[date]]:
    """First (1 − fraction) of trading days = in-sample, the rest = validation."""
    cut = int(round(len(days) * (1 - validation_fraction)))
    return days[:cut], days[cut:]


def metrics(trades: pd.DataFrame, days: list[date], capital: float) -> dict:
    t = trades[trades["day"].isin(set(days))] if not trades.empty else trades
    daily = pd.Series(0.0, index=pd.Index(days, name="day"))
    if not t.empty:
        daily = daily.add(t.groupby("day")["pnl_net"].sum(), fill_value=0.0)
    equity = daily.cumsum()
    drawdown = float((equity.cummax().clip(lower=0) - equity).max()) if len(equity) else 0.0
    n = len(t)
    if n == 0:
        return {"days": len(days), "trades": 0, "net_pnl": 0.0, "max_dd": drawdown,
                "max_dd_pct": drawdown / capital * 100}
    wins, losses = t.loc[t["pnl_net"] > 0, "pnl_net"], t.loc[t["pnl_net"] <= 0, "pnl_net"]
    return {
        "days": len(days),
        "trades": n,
        "trades_per_day": n / len(days),
        "win_rate": len(wins) / n * 100,
        "expectancy_r": float(t["r_multiple"].mean()),
        "avg_win_r": float(t.loc[t["pnl_net"] > 0, "r_multiple"].mean()) if len(wins) else 0.0,
        "avg_loss_r": float(t.loc[t["pnl_net"] <= 0, "r_multiple"].mean()) if len(losses) else 0.0,
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
        "gross_pnl": float(t["pnl_gross"].sum()),
        "costs": float(t["costs"].sum()),
        "net_pnl": float(t["pnl_net"].sum()),
        "cost_r": float((t["costs"] / t["risk_amount"]).mean()),
        "risk_pct": float((t["risk_amount"] / capital * 100).mean()),
        "cap_bound_pct": float((t["binding"] == "position_cap").mean() * 100),
        "max_dd": drawdown,
        "max_dd_pct": drawdown / capital * 100,
        "exits": dict(Counter(t["exit_reason"])),
    }


@dataclass(frozen=True)
class Criterion:
    name: str
    required: str
    value: str
    passed: bool


def criteria(val: dict, baseline_val: list[dict]) -> list[Criterion]:
    exp = val.get("expectancy_r", float("nan"))
    base = float(np.mean([b.get("expectancy_r", np.nan) for b in baseline_val])) if baseline_val else float("nan")
    pf = val.get("profit_factor", 0.0)
    return [
        Criterion("Validation trades", f">= {MIN_TRADES}", f"{val['trades']}", val["trades"] >= MIN_TRADES),
        Criterion("Expectancy (net)", f"> {MIN_EXPECTANCY_R} R", f"{exp:+.3f} R", exp > MIN_EXPECTANCY_R),
        Criterion("Profit factor (net)", f"> {MIN_PROFIT_FACTOR}", f"{pf:.2f}", pf > MIN_PROFIT_FACTOR),
        Criterion("Max drawdown", f"< {MAX_DRAWDOWN_PCT}% of capital", f"{val['max_dd_pct']:.1f}%",
                  val["max_dd_pct"] < MAX_DRAWDOWN_PCT),
        Criterion("Beats random entries", f"> baseline {base:+.3f} R", f"{exp:+.3f} R",
                  not np.isnan(base) and exp > base),
    ]


def funnel(results: list[DayResult]) -> dict:
    screen = [s for r in results for s in r.screen]
    signals = [s for r in results for s in r.signals]
    a_pass = sum(s["stage"] == "A" and s["passed"] for s in screen)
    b_pass = sum(s["stage"] == "B" and s["passed"] for s in screen)
    days = max(1, len(results))
    rejected = Counter(s["detail"].split(" (")[0] if s["outcome"] == "rejected" else s["outcome"] for s in signals
                       if s["outcome"] != "approved")
    return {"stage_a_per_day": a_pass / days, "watchlist_per_day": b_pass / days, "signals": len(signals),
            "approved": sum(s["outcome"] == "approved" for s in signals),
            "blocked": sum(s["outcome"] == "blocked" for s in signals),
            "rejections": dict(rejected.most_common(8)),
            "watchlist_symbol_days": b_pass,
            "kill_switch_days": sum(any(e["kind"] == "KILL_SWITCH" for e in r.events) for r in results)}


def _fmt(v, kind: str) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "–"
    return {"int": f"{v:,.0f}", "r": f"{v:+.3f}", "pct": f"{v:.1f}%", "rs": f"₹{v:,.0f}", "x": f"{v:.2f}"}[kind]


ROWS = [("Trades", "trades", "int"), ("Trades / day", "trades_per_day", "x"), ("Win rate", "win_rate", "pct"),
        ("Expectancy (net R)", "expectancy_r", "r"), ("Avg win (R)", "avg_win_r", "r"),
        ("Avg loss (R)", "avg_loss_r", "r"), ("Profit factor", "profit_factor", "x"),
        ("Net P&L", "net_pnl", "rs"), ("Gross P&L", "gross_pnl", "rs"), ("Costs", "costs", "rs"),
        ("Avg cost (R)", "cost_r", "x"), ("Avg risk / trade (% capital)", "risk_pct", "x"),
        ("Size set by position cap", "cap_bound_pct", "pct"), ("Max drawdown", "max_dd", "rs"),
        ("Max drawdown (% capital)", "max_dd_pct", "pct")]


def render(run_id: str, settings: Settings, days: list[date], full: dict, ins: dict, val: dict,
           baseline: dict[int, dict[str, dict]], checks: list[Criterion], fun: dict, trades: pd.DataFrame,
           note: str = "") -> str:
    verdict = "PASS" if all(c.passed for c in checks) else "FAIL"
    in_days, val_days = split_days(days, settings.backtest.validation_fraction)
    r = settings.risk
    def baseline_mean(period: str) -> dict:
        return {key: float(np.mean([b[period].get(key, np.nan) for b in baseline.values()]))
                for _, key, _ in ROWS} if baseline else {}

    base_full, base_val = baseline_mean("full"), baseline_mean("validation")

    out = [f"# Backtest {run_id} — ORB v1: **{verdict}**", ""]
    if note:
        out += [note, ""]
    out += [f"- Period: {days[0]} → {days[-1]} ({len(days)} trading days). In-sample {in_days[0]} → {in_days[-1]}, "
            f"validation {val_days[0]} → {val_days[-1]}.",
            f"- Capital ₹{settings.capital:,.0f} each day (no compounding); risk {r.risk_per_trade_pct}% per trade, "
            f"max position {r.max_position_pct}%, leverage {r.leverage:g}×; slippage "
            f"{settings.backtest.slippage_bps:g} bps on stop and market exits.",
            f"- Corporate-event hard block: {'on' if settings.backtest.block_corporate_events else 'off'}. "
            "News sentiment veto: not applied (forward-only, §9).", "",
            "## Go-live criteria (validation period)", "", "| Criterion | Required | Result | |", "|---|---|---|---|"]
    out += [f"| {c.name} | {c.required} | {c.value} | {'✅' if c.passed else '❌'} |" for c in checks]
    out += ["", "## Results", "",
            "| | Full | In-sample | Validation | Random baseline (full) | Random baseline (validation) |",
            "|---|---|---|---|---|---|"]
    for label, key, kind in ROWS:
        out.append(f"| {label} | {_fmt(full.get(key), kind)} | {_fmt(ins.get(key), kind)} | "
                   f"{_fmt(val.get(key), kind)} | {_fmt(base_full.get(key), kind)} | {_fmt(base_val.get(key), kind)} |")
    if baseline:
        seeds = ", ".join(f"{s}: {b['validation'].get('expectancy_r', float('nan')):+.3f}"
                          for s, b in baseline.items())
        out += ["", f"Baseline = random entry time and side on the same watchlist with the same stops, exits and "
                    f"risk gate; mean of {len(baseline)} seeds. Validation expectancy by seed: {seeds}."]
    out += ["", "## Exits", "", "| Exit | Full | Validation |", "|---|---|---|"]
    for reason in ("target", "stop", "breakeven", "time_exit"):
        out.append(f"| {reason} | {full.get('exits', {}).get(reason, 0)} | {val.get('exits', {}).get(reason, 0)} |")
    out += ["", "## Funnel", "",
            f"- Stage A candidates / day: {fun['stage_a_per_day']:.1f}; watchlist / day: "
            f"{fun['watchlist_per_day']:.1f}",
            f"- Signals: {fun['signals']:,}; approved {fun['approved']:,}; blocked by corporate events "
            f"{fun['blocked']:,}; kill-switch days {fun['kill_switch_days']}",
            "- Not approved, by reason: " + "; ".join(f"{k} ({v})" for k, v in fun["rejections"].items())]
    if not trades.empty:
        monthly = trades.assign(month=pd.to_datetime(trades["day"]).dt.strftime("%Y-%m")).groupby("month").agg(
            trades=("pnl_net", "size"), net=("pnl_net", "sum"), r=("r_multiple", "mean"))
        out += ["", "## By month", "", "| Month | Trades | Net P&L | Avg R |", "|---|---|---|---|"]
        out += [f"| {row.Index} | {row.trades} | ₹{row.net:,.0f} | {row.r:+.3f} |" for row in monthly.itertuples()]
    out += ["", "## Known limitations", "",
            "- ASM/GSM surveillance lists are not available historically and are not applied.",
            "- F&O stocks ('No Band') are assumed to have a fixed ±"
            f"{settings.backtest.no_band_assumed_pct:g}% band; NSE widens it in steps during big moves, so the "
            "circuit check blocks some late trades on >9% movers that live trading might allow.",
            "- Fills from 1-min bars with a conservative model (stop before target in the same bar; limits must "
            "trade through); no partial fills, no volume cap.",
            "- FYERS history is adjusted retroactively for splits/bonuses; exchange charges use today's rate.",
            "- 8 stocks merged/delisted in the window have no FYERS history (small survivorship bias, §4.4).", ""]
    return "\n".join(out)
