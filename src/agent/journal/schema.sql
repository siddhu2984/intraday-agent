-- Journal (architecture.md §4.10). Every row belongs to a run (one backtest, or one paper/live day).
-- ts is the decision time: simulated time in a backtest, wall clock (IST) otherwise.
-- detail columns hold JSON for fields that are still evolving.

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,              -- backtest | paper | live
    started_at TEXT NOT NULL,
    git_commit TEXT,
    config TEXT NOT NULL,            -- settings as JSON
    note TEXT
);

CREATE TABLE IF NOT EXISTS screen_results (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs, ts TEXT NOT NULL,
    day TEXT NOT NULL, symbol TEXT NOT NULL, stage TEXT NOT NULL,
    passed INTEGER NOT NULL,
    detail TEXT                      -- filter values and which failed
);

CREATE TABLE IF NOT EXISTS news_scores (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs, ts TEXT NOT NULL,
    symbol TEXT, input_hash TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
    output TEXT, latency_ms INTEGER, cost_usd REAL
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs, ts TEXT NOT NULL,
    symbol TEXT NOT NULL, side TEXT NOT NULL,
    entry REAL, stop REAL, target REAL, reason TEXT,
    detail TEXT                      -- features
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs, ts TEXT NOT NULL,
    symbol TEXT NOT NULL, side TEXT NOT NULL, kind TEXT NOT NULL,   -- kind: entry | exit
    approved INTEGER NOT NULL, qty INTEGER NOT NULL, reason TEXT,
    detail TEXT                      -- intent, sizing, every check's result
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs, ts TEXT NOT NULL,
    tag TEXT NOT NULL, broker_order_id TEXT,
    symbol TEXT NOT NULL, side TEXT NOT NULL, order_type TEXT, status TEXT NOT NULL,
    price REAL, qty INTEGER, filled_qty INTEGER,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs,
    symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
    entry_ts TEXT NOT NULL, entry_price REAL NOT NULL,
    exit_ts TEXT, exit_price REAL, exit_reason TEXT,
    r_multiple REAL, pnl_gross REAL, costs REAL, pnl_net REAL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs, ts TEXT NOT NULL,
    kind TEXT NOT NULL,              -- e.g. KILL_SWITCH, DATA_STALE, RECONCILE_MISMATCH, ERROR
    level TEXT NOT NULL,             -- INFO | WARN | CRITICAL
    message TEXT,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_risk_decisions_run ON risk_decisions (run_id, ts);
CREATE INDEX IF NOT EXISTS idx_system_events_run ON system_events (run_id, ts);
CREATE INDEX IF NOT EXISTS idx_trades_run ON trades (run_id, entry_ts);
