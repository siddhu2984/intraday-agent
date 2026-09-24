from datetime import datetime

from agent.broker.fyers_auth import IST
from agent.journal.db import Journal
from agent.ops.killswitch import MANUAL_FLAG, KillSwitch
from conftest import make_settings

DAY1 = datetime(2026, 9, 24, 11, 0, tzinfo=IST)
DAY1_LATER = datetime(2026, 9, 24, 14, 0, tzinfo=IST)
DAY2 = datetime(2026, 9, 25, 9, 30, tzinfo=IST)


def test_starts_clear(tmp_path):
    assert not KillSwitch(tmp_path).status(DAY1).engaged


def test_auto_engage_lasts_the_day_and_survives_a_restart(tmp_path):
    KillSwitch(tmp_path).engage("daily loss", DAY1)
    restarted = KillSwitch(tmp_path)
    status = restarted.status(DAY1_LATER)
    assert (status.engaged, status.reason, status.source) == (True, "daily loss", "auto")
    assert not restarted.status(DAY2).engaged


def test_first_reason_of_the_day_is_kept(tmp_path):
    ks = KillSwitch(tmp_path)
    ks.engage("3 consecutive losses", DAY1)
    ks.engage("daily loss", DAY1_LATER)
    assert KillSwitch(tmp_path).status(DAY1_LATER).reason == "3 consecutive losses"


def test_engage_on_a_new_day_replaces_yesterdays(tmp_path):
    ks = KillSwitch(tmp_path)
    ks.engage("old", DAY1)
    ks.engage("new", DAY2)
    assert ks.status(DAY2).reason == "new"


def test_manual_flag_stays_until_deleted(tmp_path):
    (tmp_path / MANUAL_FLAG).write_text("going on holiday", encoding="utf-8")
    ks = KillSwitch(tmp_path)
    for now in (DAY1, DAY2):
        status = ks.status(now)
        assert (status.engaged, status.reason, status.source) == (True, "going on holiday", "manual")
    (tmp_path / MANUAL_FLAG).unlink()
    assert not ks.status(DAY2).engaged


def test_empty_manual_flag_has_a_default_reason(tmp_path):
    (tmp_path / MANUAL_FLAG).touch()
    assert KillSwitch(tmp_path).status(DAY1).reason == "manual kill flag present"


def test_in_memory_mode_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ks = KillSwitch(None)
    ks.engage("daily loss", DAY1)
    assert ks.status(DAY1).engaged and not ks.status(DAY2).engaged
    assert list(tmp_path.iterdir()) == []


def test_engage_is_journaled_once(tmp_path):
    journal = Journal(":memory:")
    journal.start_run(make_settings())
    ks = KillSwitch(tmp_path, journal=journal)
    ks.engage("daily loss", DAY1)
    ks.engage("again", DAY1_LATER)
    [event] = journal.rows("system_events")
    assert (event["kind"], event["level"], event["message"]) == ("KILL_SWITCH", "CRITICAL", "daily loss")
