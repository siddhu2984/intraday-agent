# Daily runbook — a full trading day

> Companion to [architecture.md](architecture.md) and [stock-selection.md](stock-selection.md).
> Times are IST. Thresholds are the defaults from architecture.md §8.

## At a glance

| Time | Phase | Agent | You |
|---|---|---|---|
| Before 08:30 | 0. Pre-start | — | Broker login, start agent |
| 08:30–08:45 | 1. Startup checks | Calendar, clock, config, session, reconcile, reset | Check the startup message |
| 08:45–09:08 | 2. Preparation | Universe filters, news fetch, Claude scoring | — |
| 09:00–09:15 | 3. Pre-open auction | Gap ranking (Stage A), subscribe live feed | — |
| 09:15–09:30 | 4. Opening range | Build candles, VWAP, OR — **no trades** | — |
| 09:30 | 5. Watchlist | Stage B → top 10 | Check the watchlist message |
| 09:30–14:30 | 6. Trading window | Signals → veto → risk → orders; health checks | CRITICAL alerts only |
| 09:45–13:00 | 6d. Rescan *(v1.1)* | Add in-play top gainers/losers | — |
| 14:30–15:14 | 7. Wind-down | No new entries; force exit 15:10; confirm flat | Confirm "flat" message |
| 15:30+ | 8. Close & review | Reconcile, report, archive, shut down | Read the report |

---

## Phase 0 — Before 08:30 (you)

1. Switch on the PC / VPS; check the internet connection.
2. Log in to the broker so today's API session token is issued. Tokens expire daily and most brokers don't
   allow automating this login.
3. Start the agent: `python -m agent.main --mode paper` (or `live`).

## Phase 1 — Startup checks, 08:30–08:45

The agent refuses to trade if any of these fail.

| # | Check | If it fails |
|---|---|---|
| 1 | Today is an NSE trading day (holiday calendar, special sessions) | Exits: "market closed today" |
| 2 | System clock vs internet time, drift ≤ 2 s | Refuses to start, alerts you |
| 3 | Load `settings.yaml`; print mode, capital, risk limits | Stops on bad config |
| 4 | Broker session valid; funds/margin fetched | Alert: "login needed" |
| 5 | **Reconcile**: fetch positions and open orders — expect none | Leftover position is adopted, a stop attached, CRITICAL alert |
| 6 | Reset daily risk state: P&L = 0, trades = 0, loss streak = 0, kill switch off | — |
| 7 | Telegram connection | Warns only |

Telegram: *"✅ Agent started · paper · capital ₹1,00,000 · max daily loss ₹2,000"*

## Phase 2 — Preparation, 08:45–09:08

**2a. Universe (~500 → ~350)**
- Load the point-in-time Nifty 500 list.
- Drop ASM / GSM / T2T stocks, price < ₹50, 20-day avg turnover < ₹20 cr, price band ≤ 5%.
- Every removal and its reason → `screen_results`.

**2b. News**
- Fetch headlines since yesterday's close.
- Results or corporate action today → **hard block** for the day.
- Stocks with headlines → Claude in batch → `bullish | bearish | neutral` + confidence.
- Cached and logged (`news_scores`); used later by the veto.
- News API or Claude down → log `NEWS_UNAVAILABLE`, continue without the veto.

**2c. Volume profiles** *(v1.1)*
- Precompute each stock's 20-day average cumulative volume per minute (for RVOL-to-time in the rescan).

## Phase 3 — Pre-open auction, 09:00–09:15

- NSE collects pre-open orders 09:00–09:08 and sets an opening price at ~09:08.
- **09:08** — gap % = (opening price − previous close) / previous close for each stock.
- Rank by absolute gap and keep ~40 (**Stage A**).
- **09:14** — subscribe the WebSocket for those ~40 plus the Nifty index.

## Phase 4 — Opening range, 09:15–09:30 (watch only)

- Ticks → 1-min and 5-min candles; VWAP updated per stock.
- Track each stock's **high/low** (the opening range), **volume so far**, and Nifty's move.
- Staleness monitor starts: no tick for 20 s → stock marked stale.
- **No orders.** The first 15 minutes are mostly noise.

## Phase 5 — Watchlist at 09:30 (Stage B)

| Check | Rule |
|---|---|
| RVOL | First-15-min volume ÷ 20-day average for the same window ≥ 2.0 |
| ATR% | Between 1% and 5% |
| Relative strength | Stock % move − Nifty % move. Positive → **longs only**; negative → **shorts only** |

- Survivors ranked by RVOL (ties by |RS|); **top 10 = watchlist**.
- Live feed for the rest is dropped.
- Telegram: *"📋 Watchlist: XYZ (L), ABC (S), …"*

## Phase 6 — Trading window, 09:30–14:30

Loops A–C run concurrently; D is added in v1.1.

### 6a. Signal loop — on every 5-min candle close, per watchlist stock
1. Candle **closed** above OR high (long) / below OR low (short)? If not, next stock.
2. Price on the right side of VWAP and RS agrees? If not → logged "not confirmed".
3. Already traded this stock today? Limit is 1 per stock.
4. Compute entry, stop, target. Stop must be ≥ 0.3% and ≤ 1.5 × ATR, else dropped.
5. **News veto** — cached score opposes the trade with confidence ≥ 70 → blocked and tracked in shadow.
6. **Risk gate** — all must pass:
   - kill switch off, data healthy, reconcile OK
   - daily P&L (realized + open) above −2%
   - < 3 open positions, < 5 trades today, < 3 consecutive losses
   - no open position in the same sector
   - enough margin (≤ 80% of available)
   - not within 1% of the circuit limit

   Then compute quantity = min(risk budget ÷ stop distance, position cap).
7. Hand the approved order to the order manager.

### 6b. Trade loop — per trade
1. Place a **limit entry** with our own order ID; wait up to 60 s. Not filled → cancel and log.
2. **On fill (full or partial)** → immediately place a **broker-side stop-loss** for the filled quantity.
   Fails twice → market exit + CRITICAL alert.
3. Place the **target** order at 2R.
4. Price reaches +1R → move the stop to breakeven.
5. Stop or target fills → cancel the other, confirm flat with the broker, record the trade
   (P&L, R multiple, costs, exit reason).
6. Telegram on every fill and exit.

### 6c. Health loop — every few seconds
| Condition | Action |
|---|---|
| Daily P&L ≤ −2% or 3 losses in a row | **Kill switch** for the day: no new entries, open trades keep their stops |
| Reconcile (every 30 s) finds a mismatch | Block new entries, CRITICAL alert |
| Stale data / WebSocket drop | Block new entries, reconnect with backoff |
| New headline for a watchlist stock | Score it, update the cached verdict |
| Your command | `/status` · `/kill` · `/flatten` (exit everything now) |

### 6d. Rescan loop — every 15 min, 09:45–13:00 *(v1.1, off in v1)*
1. Batch-quote ~350 filtered stocks; take top 10 gainers + top 10 losers by % change.
2. Keep only: RVOL-to-time ≥ 2, RS same direction, move ≤ 7%, within 2 × ATR of VWAP, away from circuit,
   not already watched or traded.
3. Add to the watchlist (max 15); remove if no trigger within 60 min.
4. Score their headlines.
5. Entries use **VWAP pullback** or **fresh consolidation breakout** instead of the opening range; steps
   5–7 of 6a and all of 6b are unchanged.

## Phase 7 — Wind-down, 14:30–15:14

| Time | Action |
|---|---|
| 14:30 | **No new entries.** Pending entry orders cancelled. Open trades continue |
| 15:10 | **Force exit**: cancel all stop/target orders, close every position at market |
| 15:12 | Confirm flat with the broker |
| 15:14 | Still open → CRITICAL alert, close manually. Otherwise the broker's auto square-off follows (with a fee) |

## Phase 8 — Close and review, 15:30+

**Agent**
1. Final reconcile: broker trade book vs journal.
2. Daily report (journal + Telegram):
   - trades, win rate, average R, gross and net P&L after costs
   - vetoed trades and their shadow outcomes
   - how many stocks were rejected at each stage
   - results per setup (`orb`, and in v1.1 `vwap_pullback`, `consol_breakout`)
   - system events: reconnects, rejected orders, kill-switch triggers
3. Archive the day's 1-min candles for backtests.
4. Shut down cleanly.

**You (10–15 min)**
- Read the report; look over each losing trade; note anything unusual.
- **Don't change settings after one day.** Adjust only at the weekly review, with enough trades.

## Your role summary

| When | You |
|---|---|
| Before 08:30 | Broker login, start the agent |
| 08:30–09:30 | Check the startup and watchlist messages |
| During the day | Respond to CRITICAL alerts only; `/kill` if something looks wrong |
| 15:14 | Confirm you received the "flat" message |
| After close | Read the daily report |
| Weekly | Review the week before changing any parameter |

## Emergency quick reference

| Situation | Do this |
|---|---|
| Something looks wrong, unsure what | `/kill` — stops new entries, stops stay in place |
| Want out of everything now | `/flatten` |
| Agent crashed | Restart it — the reconciler rebuilds state; broker-side stops protected open trades meanwhile |
| Not flat at 15:14 | Close manually in the broker app |
| Internet down | Broker-side stops still protect positions; close manually from the broker app on mobile if needed |
