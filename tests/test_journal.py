import json
from datetime import date, datetime, time

import pytest

from agent.broker.fyers_auth import IST
from agent.journal.db import Journal
from agent.ops.calendar import NseCalendar
from agent.risk.gate import check_entry, check_exit
from agent.risk.state import EntryIntent, ExitIntent, RiskState, Side
from conftest import make_settings

SETTINGS = make_settings()
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=IST)


@pytest.fixture
def journal():
    j = Journal(":memory:")
    j.start_run(SETTINGS, mode="backtest", note="test")
    yield j
    j.close()


def test_run_records_mode_and_config(journal):
    run = journal._db.execute("SELECT * FROM runs WHERE run_id = ?", (journal.run_id,)).fetchone()
    assert run["mode"] == "backtest"
    assert json.loads(run["config"])["risk"]["risk_per_trade_pct"] == 0.5


def test_writing_before_a_run_starts_fails():
    j = Journal(":memory:")
    with pytest.raises(RuntimeError, match="start_run"):
        j.log_event("ERROR", "WARN", "x")


def test_log_event(journal):
    journal.log_event("DATA_STALE", "WARN", "no ticks for 20 s", {"symbols": ["NSE:SBIN-EQ"]}, ts=NOW)
    [row] = journal.rows("system_events")
    assert (row["kind"], row["level"], row["ts"]) == ("DATA_STALE", "WARN", NOW.isoformat())
    assert json.loads(row["detail"]) == {"symbols": ["NSE:SBIN-EQ"]}
    with pytest.raises(ValueError):
        journal.log_event("X", "FATAL", "bad level")


def test_log_risk_decisions_keeps_every_check_and_the_sizing(journal):
    calendar = NseCalendar({date(2026, 1, 26): "Republic Day"}, {})
    state = RiskState(NOW, 100_000, 100_000, 0, 0, 0, 5)
    intent = EntryIntent("NSE:INFY-EQ", "Information Technology", Side.LONG, 1000, 990, 1100, 900)
    journal.log_risk_decision(intent, check_entry(intent, state, SETTINGS, calendar), ts=NOW)
    exit_intent = ExitIntent("NSE:INFY-EQ", Side.LONG, 5)
    journal.log_risk_decision(exit_intent, check_exit(exit_intent, state), ts=NOW)

    entry, exit_ = journal.rows("risk_decisions")
    assert (entry["kind"], entry["side"], entry["approved"], entry["qty"]) == ("entry", "long", 0, 0)
    detail = json.loads(entry["detail"])
    assert detail["intent"]["side"] == "long"
    assert detail["sizing"]["binding"] == "position_cap"
    assert [c["name"] for c in detail["checks"] if not c["passed"]] == ["trades_per_day"]
    assert (exit_["kind"], exit_["approved"]) == ("exit", 0)


def test_rows_are_scoped_to_the_run(journal):
    journal.log_event("A", "INFO", "first run")
    journal.start_run(SETTINGS)
    journal.log_event("B", "INFO", "second run")
    assert [r["kind"] for r in journal.rows("system_events")] == ["B"]


def test_file_journal_persists(tmp_path):
    path = tmp_path / "journal.db"
    j = Journal(path)
    run_id = j.start_run(SETTINGS)
    j.log_event("A", "INFO", "hello")
    j.close()
    j = Journal(path)
    j.run_id = run_id
    assert len(j.rows("system_events")) == 1
    j.close()
