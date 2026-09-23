# How a stock is decided (intraday)

> Companion to [architecture.md](architecture.md). Thresholds are the defaults from §8 of that doc —
> starting hypotheses to be validated by backtest, not recommendations.

## 1. The decision funnel

```
~500 stocks (Nifty 500)
   │  Stage A · 08:45–09:10   hard filters + gap          → ~30–40
   │  Stage B · 09:30         volume, volatility, strength → watchlist of 10
   │  Trigger · 09:30–14:30   breakout confirmed on chart  → 0–5 signals/day
   │  News veto               Claude can block, never add
   │  Risk gate               can we afford it? how much?  → quantity
   ▼
Order at the broker (entry + broker-side stop + target)
```

Each stage answers one question:

| Stage | Question |
|---|---|
| A | Is this stock **safe and liquid** enough to trade at all? |
| B | Is something **unusual happening** in it today? |
| Trigger | Has price **confirmed a direction** right now? |
| News veto | Does the news **contradict** that direction? |
| Risk gate | Can **the account** take this trade, and how big? |

## 2. Deciding factors

| # | Factor | Stage | Role | Default | Why it matters |
|---|---|---|---|---|---|
| 1 | Surveillance lists (ASM/GSM), trade-to-trade (T2T) | A | Filter | Excluded | Extra margin, no intraday allowed, or erratic moves |
| 2 | Price | A | Filter | ≥ ₹50 | Penny stocks move in big jumps and have wide bid/ask spreads |
| 3 | 20-day avg turnover | A | Filter | ≥ ₹20 cr/day | You can get in and out without moving the price |
| 4 | Price band (circuit limit) | A | Filter | Band > 5% | A tight band can lock the stock at its limit so you can't exit |
| 5 | Pre-open gap % | A | Rank | Largest gaps first | Overnight news or a buy/sell imbalance — a reason to move today |
| 6 | Relative volume (RVOL) | B | Filter + rank | ≥ 2.0 | Big participants are active today. Compares the first 15 min today with the **same 15 min** on past days, because every open is busy |
| 7 | ATR % | B | Filter | 1–5% | Below 1%, a winner won't cover costs. Above 5%, too wild for a sensible stop |
| 8 | Relative strength vs Nifty | B + trigger | Rank + direction | > 0 long, < 0 short | Long the stocks beating the market, short the laggards |
| 9 | Opening range (09:15–09:30 high/low) | Trigger | Level | — | The first 15 min set the battle lines; a breakout shows one side has won |
| 10 | 5-min candle **close** beyond the range | Trigger | Entry | — | Waiting for the close filters out spikes that reverse |
| 11 | VWAP | Trigger | Confirmation | Long above, short below | Institutional benchmark; above VWAP means today's buyers are in profit |
| 12 | News sentiment (Claude) | Veto | Block only | Opposite view, confidence ≥ 70 | Avoids buying into bad news |
| 13 | Results or corporate action today | Veto | Hard block | — | Price can jump unpredictably |
| 14 | Stop distance | Strategy + risk | Filter + sizing | ≥ 0.3%, ≤ 1.5×ATR | Too tight gets stopped by noise; too wide makes risk too big |
| 15 | Account state | Risk | Filter | See architecture.md §4.7 | Protects the account however good the signal looks |

- Factors 1–4 are **pass/fail**, 5–8 **rank**, 9–11 **trigger**, 12–13 can only **block**, 14–15 decide **whether and how big**.
- Watchlist ranking: Stage B survivors are ranked by RVOL, ties broken by |RS|; the top 10 are watched.

## 3. What each component does

| Component | Input → Output | Job |
|---|---|---|
| Broker adapter | API calls → ticks, candles, orders, positions | Wraps the broker API, applies rate limits, gives each order a unique ID so retries are safe. `SimBroker` stands in for backtest and paper |
| Market data | Ticks → 1/5-min candles, VWAP, ATR, opening range | Turns raw prices into indicators; flags stale data and bad ticks |
| News adapter | News API → deduplicated headlines per ticker | Fetches only, no interpretation |
| Screener | Universe + candles → watchlist of 10 | Stages A and B (factors 1–8) |
| Strategy (ORB) | Watchlist candles → `Signal(side, entry, stop, target)` | Factors 9–11 and 14. Rules only |
| News veto | Headlines → cached verdict → pass/block | Factors 12–13. Runs pre-market, never in the order path |
| Risk gate | Signal + account state → quantity or rejection reason | Factor 15 and sizing. Can say no to anything |
| Order manager (OMS) | Approved order → broker orders → trade states | Entry, broker-side stop, target, cancelling the other exit, partial fills, 15:10 exit |
| Reconciler | Broker state vs bot state | Broker is the source of truth; on mismatch, stop new entries and alert |
| Journal | Every event → SQLite | Why each stock was or wasn't traded; audit trail |
| Alerts / kill switch | Events → Telegram; `/kill`, `/status`, `/flatten` back | Human oversight |
| Scheduler | Clock + NSE calendar → timed jobs | Runs each stage on time, skips holidays, checks clock drift |

## 4. Worked example: stock "XYZ", capital ₹1,00,000

**08:45 — Stage A.** Prev close ₹500. Not on a surveillance list, avg turnover ₹80 cr/day, price
band 20% → passes. At 09:08 the pre-open price is ₹510 → **+2.0% gap**, ranks in the top 40, gets a
live feed from 09:15.

**09:30 — Stage B.**
- Opening range: high ₹515, low ₹506.
- Volume: 3.0 lakh shares in the first 15 min vs 1.2 lakh 20-day average for the same window → **RVOL 2.5** ✓ (≥ 2.0).
- Volatility: ATR% 2.4% ✓ (1–5%). 5-min ATR = ₹4.00.
- Strength: XYZ +2.8% vs Nifty +0.4% → **RS positive**, longs only.

Ranks #3 by RVOL → on the **watchlist**.

**10:05 — Trigger.** A 5-min candle **closes at ₹517.50**, above the ₹515 range high; VWAP is
₹511.80, price above it → **long signal**.

| Item | Calculation | Value |
|---|---|---|
| Entry (limit) | 517.50 + 0.05% buffer | ₹517.75 |
| Stop | Breakout candle low | ₹513.00 |
| 1R (risk per share) | 517.75 − 513.00 | ₹4.75 |
| Min stop check | ≥ 0.3% × 517.75 = ₹1.55 | ✓ |
| Max stop check | ≤ 1.5 × ATR = ₹6.00 | ✓ |
| Breakeven trigger (+1R) | 517.75 + 4.75 | ₹522.50 |
| Target (2R) | 517.75 + 9.50 | ₹527.25 |

**News veto.** Headline "XYZ wins ₹1,200 cr order" → Claude: **bullish, 80**. Agrees with the long
→ passes. (Bearish ≥ 70 would have blocked it and logged it for shadow tracking.)

**Risk gate.**

| Item | Calculation | Value |
|---|---|---|
| Risk budget | 0.5% × ₹1,00,000 | ₹500 |
| Qty by risk | floor(500 / 4.75) | 105 |
| Qty by position cap | floor(20% × ₹1,00,000 × 1 / 517.75) | **38** |
| Final qty | min(105, 38) | **38** |
| Actual risk | 38 × 4.75 | ₹180.50 |

Other checks: daily P&L OK, 1 of 3 positions open, no same-sector position, margin available,
before 14:30 → **approved**.

At ₹1 lakh without leverage the position cap, not the risk budget, usually sets the size.

**Execution.**
1. Buy limit 38 @ ₹517.75 → filled.
2. Immediately place a broker-side stop-loss sell, trigger ₹513.00.
3. Place a target limit sell @ ₹527.25. When one exit fills, the bot cancels the other.

**Outcomes** (round-trip costs ≈ ₹35–45 depending on broker):

| Outcome | Gross | Net (approx.) |
|---|---|---|
| Target hit | +₹361 | +₹320 |
| Stopped out | −₹180.50 | −₹220 |
| Breakeven stop after +1R | ₹0 | −₹40 |
| Neither by 15:10 | Market exit at current price | — |

Every step writes a journal row: screen result, news score, signal, risk decision, orders, trade.

## 5. How other stocks fail

| Stock | What happened | Rejected at |
|---|---|---|
| ABC | On the ASM list | Stage A |
| DEF | Gapped +3% but RVOL 1.3 — gap without real buying | Stage B |
| GHI | Broke the range high while below VWAP | Trigger (not confirmed) |
| JKL | Valid long, but "auditor resigns" headline → bearish 85 | News veto |
| MNO | Valid signal, but daily loss already −2% | Risk gate (kill switch for the day) |
| PQR | Valid signal at 14:45 | Risk gate (after last entry time) |

## 6. Top gainers / losers (v1.1 — see architecture.md §4.4b)

**Already covered in v1:** Stage A's gap ranking is the pre-open top gainers/losers list; Stage B's relative
strength is top gainers/losers net of the market. What v1 misses is a stock that only **becomes** a top
gainer after 09:30.

**Why not just buy the top gainers list:** by the time a stock is on it, much of the move is done; it's
often far above VWAP (wide stop, small size, poor R:R); many gainers sit near their upper circuit; and the
list is dominated by illiquid small caps.

**v1.1 rescan, every 15 min from 09:45 to 13:00:**

```
~350 filtered stocks
   │  top 10 gainers + top 10 losers by % change
   │  guards: RVOL-to-time ≥ 2 · RS same direction · move ≤ 7% · within 2×ATR of VWAP · away from circuit
   ▼
added to watchlist (max 15) · expires after 60 min without a trigger
   │  entry: VWAP pullback  or  fresh 15-min consolidation breakout
   ▼
news veto → risk gate → order   (same as ORB)
```

**Example — stock "RST".** Flat at 09:30, so not on the watchlist. At 11:00 it announces a large order and
by the 11:15 scan it's +4.1% with RVOL-to-time 3.4, RS positive, 1.3×ATR above VWAP, far from its circuit →
**added, longs only**. At 11:40 it dips to within 0.2% of VWAP, then a 5-min candle closes back above VWAP
and above the previous candle's high → **VWAP-pullback long**, stop at the pullback low, target 2R.

**How the same scan rejects others:**

| Stock | What happened | Guard that fails |
|---|---|---|
| UVW | +9.5% | Move > 7% — too stretched |
| XYA | +4%, RVOL-to-time 1.2 | No real participation |
| BCD | +5%, 3.1×ATR above VWAP | Too extended |
| EFG | +2.0% on a day Nifty is +2.3% | RS negative — just riding the index |
| HIJ | Added at 11:15, no trigger by 12:15 | Expired (TTL) |
