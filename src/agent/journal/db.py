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
                  started_at: datetime | None = None) -> str:
        """Start a run; every later row is tagged with its run_id."""
        started = started_at or datetime.now(IST)
        self.run_id = f"{started:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        self._insert("runs", {
            "run_id": self.run_id, "mode": mode or settings.mode, "started_at": started.isoformat(),
            "git_commit": _git_commit(), "config": to_json(to_dict(settings)), "note": note,
        })
        return self.run_id

    def _insert(self, table: str, row: dict) -> None:
        columns = ", ".join(row)
        placeholders = ", ".join("?" * len(row))
        with self._db:
            self._db.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", list(row.values()))

    def _run_row(self, ts: datetime | None) -> dict:
        if self.run_id is None:
            raise RuntimeError("call start_run() before writing to the journal")
        return {"run_id": self.run_id, "ts": (ts or datetime.now(IST)).isoformat()}

    def log_event(self, kind: str, level: str, message: str, detail=None, ts: datetime | None = None) -> None:
        if level not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS}")
        self._insert("system_events", {**self._run_row(ts), "kind": kind, "level": level, "message": message,
                                        "detail": to_json(detail) if detail is not None else None})

    def log_risk_decision(self, intent: EntryIntent | ExitIntent, decision, ts: datetime | None = None) -> None:
        """An EntryIntent or ExitIntent and the gate's Decision, with every check and the sizing."""
        kind = "exit" if isinstance(intent, ExitIntent) else "entry"
        self._insert("risk_decisions", {
            **self._run_row(ts), "symbol": intent.symbol, "side": intent.side.value, "kind": kind,
            "approved": int(decision.approved), "qty": decision.qty, "reason": decision.reason,
            "detail": to_json({"intent": intent, "sizing": decision.sizing, "checks": decision.checks}),
        })

    def rows(self, table: str, **where) -> list[dict]:
        """Rows of one table for the current run (for reports and tests)."""
        clauses = ["run_id = ?"] + [f"{k} = ?" for k in where]
        sql = f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} ORDER BY rowid"
        return [dict(r) for r in self._db.execute(sql, [self.run_id, *where.values()])]
