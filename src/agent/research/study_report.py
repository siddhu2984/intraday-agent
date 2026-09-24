"""Event-study report: per news group and horizon, gross and net follow-through, vs no-news controls with the same
reaction size, in both halves of the period. A row makes the shortlist when it could support a strategy."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from agent.backtest.costs import round_trip
from agent.backtest.report import split_days
from agent.config import Settings
from agent.risk.state import Side

HORIZONS = {"session": ["15m", "60m", "eod"], "overnight": ["0920_60m", "0920_eod", "0930_60m", "0930_eod"]}
BUCKETS = 5
# shortlist: enough events, positive net in both halves, significant, better than controls
MIN_EVENTS, MIN_T = 100, 2.0


def cost_pct(settings: Settings, notional: float = 20_000) -> float:
    """Round-trip charges + slippage on both legs, % of notional."""
    charges = round_trip(Side.LONG, int(notional / 1000), 1000, 1000, settings.costs).total / notional * 100
    return charges + 2 * settings.backtest.slippage_bps / 100


def add_buckets(df: pd.DataFrame) -> pd.DataFrame:
    """|reaction| quintiles, computed per timing over news and controls together."""
    df = df.copy()
    df["bucket"] = df.groupby("timing")["reaction_pct"].transform(
        lambda s: pd.qcut(s.abs(), BUCKETS, labels=False, duplicates="drop"))
    return df


def _stats(values: pd.Series) -> dict:
    v = values.dropna()
    n = len(v)
    if n < 2:
        return {"n": n, "mean": np.nan, "t": np.nan, "hit": np.nan}
    return {"n": n, "mean": v.mean(), "t": v.mean() / v.std(ddof=1) * np.sqrt(n), "hit": (v > 0).mean() * 100}


def evaluate(df: pd.DataFrame, cost: float, in_days: set[date]) -> pd.DataFrame:
    """One row per (timing, group, horizon) plus an 'all news' row, with the control benchmark matched on bucket."""
    rows = []
    for timing, horizons in HORIZONS.items():
        t = df[df["timing"] == timing]
        if t.empty:
            continue
        news, control = t[t["news"]], t[~t["news"]]
        for h in horizons:
            col = f"ret_{h}"
            control_by_bucket = (control[col] - cost).groupby(control["bucket"]).mean()
            for group, g in [("all news", news), *news.groupby("group")]:
                net = g[col] - cost
                s = _stats(net)
                weights = g["bucket"].value_counts(normalize=True)
                matched = float((weights * control_by_bucket.reindex(weights.index)).sum())
                in_mask = g["day"].isin(in_days)
                rows.append({"timing": timing, "group": group, "horizon": h, "n": s["n"],
                             "gross": g[col].mean(), "net": s["mean"], "t": s["t"], "hit": s["hit"],
                             "control": matched, "edge": s["mean"] - matched,
                             "net_in": net[in_mask].mean(), "net_val": net[~in_mask].mean()})
    out = pd.DataFrame(rows)
    out["shortlist"] = ((out["n"] >= MIN_EVENTS) & (out["net_in"] > 0) & (out["net_val"] > 0) & (out["t"] > MIN_T)
                        & (out["edge"] > 0))
    return out


def by_bucket(df: pd.DataFrame, cost: float) -> pd.DataFrame:
    """All news vs controls by reaction-size bucket, at each timing's end-of-day horizon."""
    rows = []
    for timing, col in (("session", "ret_eod"), ("overnight", "ret_0930_eod")):
        t = df[df["timing"] == timing]
        for b, g in t.groupby("bucket"):
            news, ctrl = g[g["news"]], g[~g["news"]]
            rows.append({"timing": timing, "bucket": int(b) + 1,
                         "reaction": f"{g['reaction_pct'].abs().min():.2f}–{g['reaction_pct'].abs().max():.2f}%",
                         "news_n": len(news), "news_net": (news[col] - cost).mean(),
                         "control_n": len(ctrl), "control_net": (ctrl[col] - cost).mean()})
    return pd.DataFrame(rows)


def _f(v, fmt="{:+.3f}") -> str:
    return "–" if v is None or (isinstance(v, float) and np.isnan(v)) else fmt.format(v)


def render(run_id: str, df: pd.DataFrame, days: list[date], settings: Settings) -> str:
    cost = cost_pct(settings)
    in_days, val_days = split_days(days, settings.backtest.validation_fraction)
    df = add_buckets(df)
    table = evaluate(df, cost, set(in_days))
    buckets = by_bucket(df, cost)
    news = df[df["news"]]
    shortlist = table[table["shortlist"]]

    out = [f"# News event study {run_id}", "",
           f"- Period {days[0]} → {days[-1]} ({len(days)} sessions); in-sample to {in_days[-1]}, validation from "
           f"{val_days[0]}.",
           f"- Events: {int((news['timing'] == 'session').sum()):,} session, "
           f"{int((news['timing'] == 'overnight').sum()):,} overnight; controls: "
           f"{int((~df['news']).sum()):,} stock-days without material filings.",
           f"- All returns in % of the position, in the reaction's direction. Net = gross − {cost:.3f}% "
           f"(FYERS charges on ₹20k + {settings.backtest.slippage_bps:g} bps slippage on entry and exit).",
           "- *Control* = the same trade on stock-days without news, matched on reaction size; *edge* = net − control.",
           f"- **Shortlist rule:** n ≥ {MIN_EVENTS}, net > 0 in both halves, t > {MIN_T:g}, edge > 0.", ""]
    out += ["## Shortlist", ""]
    if shortlist.empty:
        out += ["**Nothing qualifies.** No news group, timing and horizon shows follow-through that beats costs in "
                "both halves.", ""]
    else:
        out += ["| Timing | Group | Horizon | n | Net % | t | Edge % | In-sample | Validation |", "|---|---|---|---|---|---|---|---|---|"]
        out += [f"| {r.timing} | {r.group} | {r.horizon} | {r.n} | {_f(r.net)} | {_f(r.t, '{:.1f}')} | "
                f"{_f(r.edge)} | {_f(r.net_in)} | {_f(r.net_val)} |" for r in shortlist.itertuples()]
        out += [""]

    for timing, horizons in HORIZONS.items():
        t = table[table["timing"] == timing]
        if t.empty:
            continue
        out += [f"## {timing.capitalize()} events — net % (t-stat) · edge vs control", "",
                "| Group | n | " + " | ".join(horizons) + " |", "|---|---|" + "---|" * len(horizons)]
        groups = ["all news"] + sorted(set(t["group"]) - {"all news"},
                                       key=lambda g: -int(t.loc[t["group"] == g, "n"].max()))
        for g in groups:
            rows = t[t["group"] == g].set_index("horizon")
            cells = [f"{_f(rows.loc[h, 'net'])} ({_f(rows.loc[h, 't'], '{:.1f}')}) · {_f(rows.loc[h, 'edge'])}"
                     for h in horizons]
            out.append(f"| {g} | {int(rows['n'].max())} | " + " | ".join(cells) + " |")
        control = df[(df["timing"] == timing) & ~df["news"]]
        cells = [_f((control[f"ret_{h}"] - cost).mean()) for h in horizons]
        out += [f"| *no news (all)* | {len(control)} | " + " | ".join(cells) + " |", ""]

    out += ["## By reaction size (end-of-day exit; session: from the reaction, overnight: 09:30 entry)", "",
            "| Timing | Bucket | Reaction | News n | News net % | Control n | Control net % |",
            "|---|---|---|---|---|---|---|"]
    out += [f"| {r.timing} | {r.bucket} | {r.reaction} | {r.news_n} | {_f(r.news_net)} | {r.control_n} | "
            f"{_f(r.control_net)} |" for r in buckets.itertuples()]
    out += ["", "## Notes", "",
            "- Groups come from NSE's category plus keyword rules (config/news/categories.yaml); the direction of the "
            "news is unknown here — the trade follows the market's first reaction.",
            "- Overnight spike (first-15-min RVOL) is known only at 09:30.",
            "- Filings public between 09:15–09:20 and 15:00–15:30 are not studied.", ""]
    return "\n".join(out)
