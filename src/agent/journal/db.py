"""SQLite journal (architecture.md §4.10): every decision, not just every trade.

Writers for screen results, news scores, signals, orders and trades arrive with the phases that produce them.
"""

from __future__ import annotations

import dataclasses
import json
import secrets
import sqlite3
import subprocess
from datetime import datetime
from enum import Enum
from pathlib import Path

from agent.broker.fyers_auth import IST, PROJECT_ROOT
from agent.config import Settings, to_dict
from agent.risk.state import EntryIntent, ExitIntent

DEFAULT_PATH = PROJECT_ROOT / "data" / "journal.db"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
LEVELS = ("INFO", "WARN", "CRITICAL")


def _json_default(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Enum):
        return value.value
    return str(value)  # datetime, date, time


def to_json(value) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False)


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, capture_output=True,
                              text=True, timeout=5, check=True).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def new_run_id(started: datetime) -> str:
    return f"{started:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


class Journal:
    def __init__(self, path: Path | str = DEFAULT_PATH):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.run_id: str | None = None

    def close(self) -> None:
        self._db.close()

    def start_run(self, settings: Settings, mode: str | None = None, note: str = "",
                  started_at: datetime | None = None, run_id: str | None = None) -> str:
        """Start a run; every later row is tagged with its run_id."""
        started = started_at or datetime.now(IST)
        self.run_id = run_id or new_run_id(started)
        self._insert("runs", {
            "run_id": self.run_id, "mode": mode or settings.mode, "started_at": started.isoformat(),
            "git_commit": _git_commit(), "config": to_json(to_dict(settings)), "note": note,
        })
        return self.run_id

    def _insert(self, table: str, row: dict) -> None:
        self._insert_many(table, [row])

    def _insert_many(self, table: str, rows: list[dict]) -> None:
        """Rows with the same keys, in one transaction."""
        if not rows:
            return
        columns = ", ".join(rows[0])
        placeholders = ", ".join("?" * len(rows[0]))
        with self._db:
            self._db.executemany(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                                 [list(r.values()) for r in rows])

    def _run_row(self, ts: datetime | None) -> dict:
        if self.run_id is None:
            raise RuntimeError("call start_run() before writing to the journal")
        return {"run_id": self.run_id, "ts": (ts or datetime.now(IST)).isoformat()}

    def log_event(self, kind: str, level: str, message: str, detail=None, ts: datetime | None = None) -> None:
        if level not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS}")
        self._insert("system_events", {**self._run_row(ts), "kind": kind, "level": level, "message": message,
                                        "detail": to_json(detail) if detail is not None else None})

    def _risk_row(self, intent: EntryIntent | ExitIntent, decision, ts: datetime | None) -> dict:
        return {
            **self._run_row(ts), "symbol": intent.symbol, "side": intent.side.value,
            "kind": "exit" if isinstance(intent, ExitIntent) else "entry",
            "approved": int(decision.approved), "qty": decision.qty, "reason": decision.reason,
            "detail": to_json({"intent": intent, "sizing": decision.sizing, "checks": decision.checks}),
        }

    def log_risk_decision(self, intent: EntryIntent | ExitIntent, decision, ts: datetime | None = None) -> None:
        """An EntryIntent or ExitIntent and the gate's Decision, with every check and the sizing."""
        self._insert("risk_decisions", self._risk_row(intent, decision, ts))

    # --- bulk writers for backtest results (dict rows from agent.backtest.engine.DayResult) ---

    def log_risk_decisions(self, items: list[tuple]) -> None:
        """(intent, decision, ts) triples."""
        self._insert_many("risk_decisions", [self._risk_row(*item) for item in items])

    def log_screen_results(self, rows: list[dict]) -> None:
        self._insert_many("screen_results", [
            {**self._run_row(r["ts"]), "day": r["day"].isoformat(), "symbol": r["symbol"], "stage": r["stage"],
             "passed": int(r["passed"]), "detail": to_json({"reason": r["reason"], **r["detail"]})}
            for r in rows])

    def log_signals(self, rows: list[dict]) -> None:
        self._insert_many("signals", [
            {**self._run_row(r["ts"]), "symbol": r["symbol"], "side": r["side"], "entry": r["entry"],
             "stop": r["stop"], "target": r["target"], "reason": r["reason"],
             "detail": to_json({"outcome": r["outcome"], "detail": r["detail"], "qty": r.get("qty"),
                                "features": r["features"]})}
            for r in rows])

    def log_trades(self, rows: list[dict]) -> None:
        if self.run_id is None:
            raise RuntimeError("call start_run() before writing to the journal")
        core = ("symbol", "side", "qty", "entry_ts", "entry_price", "exit_ts", "exit_price", "exit_reason",
                "r_multiple", "pnl_gross", "costs", "pnl_net")
        self._insert_many("trades", [
            {"run_id": self.run_id,
             **{k: (r[k].isoformat() if hasattr(r[k], "isoformat") else r[k]) for k in core},
             "detail": to_json({k: v for k, v in r.items() if k not in core})}
            for r in rows])

    def rows(self, table: str, **where) -> list[dict]:
        """Rows of one table for the current run (for reports and tests)."""
        clauses = ["run_id = ?"] + [f"{k} = ?" for k in where]
        sql = f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} ORDER BY rowid"
        return [dict(r) for r in self._db.execute(sql, [self.run_id, *where.values()])]
