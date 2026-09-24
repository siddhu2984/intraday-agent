import json
from datetime import time

import pytest
import yaml

from agent.config import DEFAULT_PATH, ConfigError, load_settings, to_dict, validate
from conftest import make_settings, settings_dict


def write(tmp_path, data):
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_the_real_settings_file_loads():
    s = load_settings(DEFAULT_PATH)
    assert s.mode in ("backtest", "paper", "live")
    assert isinstance(s.strategy.last_entry, time)


def test_types_are_coerced(tmp_path):
    s = load_settings(write(tmp_path, settings_dict()))
    assert s.strategy.last_entry == time(14, 30)
    assert s.risk.leverage == 1.0 and isinstance(s.risk.leverage, float)  # YAML int → float field
    assert s.risk.max_open_positions == 3


def test_unknown_key_is_rejected(tmp_path):
    data = settings_dict(risk={"risk_per_trade": 0.5})  # typo of risk_per_trade_pct
    with pytest.raises(ConfigError, match="unknown key risk.risk_per_trade"):
        load_settings(write(tmp_path, data))


def test_missing_key_is_rejected(tmp_path):
    data = settings_dict()
    del data["risk"]["max_per_sector"]
    with pytest.raises(ConfigError, match="missing key risk.max_per_sector"):
        load_settings(write(tmp_path, data))


def test_unquoted_time_is_rejected(tmp_path):
    # YAML 1.1 reads an unquoted 14:30 as the base-60 integer 870
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings_dict()).replace("'14:30'", "14:30"), encoding="utf-8")
    with pytest.raises(ConfigError, match="strategy.last_entry: expected a time"):
        load_settings(path)


@pytest.mark.parametrize("section, key, value", [
    ("risk", "max_trades_per_day", 2.5),   # float for int
    ("risk", "max_trades_per_day", True),  # bool is not an int here
    ("news", "enabled", "yes"),
    ("risk", "leverage", "1"),
])
def test_wrong_types_are_rejected(tmp_path, section, key, value):
    with pytest.raises(ConfigError, match=f"{section}.{key}"):
        load_settings(write(tmp_path, settings_dict(**{section: {key: value}})))


@pytest.mark.parametrize("sections, message", [
    ({"mode": "live-ish"}, "mode must be"),
    ({"capital": 0}, "capital"),
    ({"risk": {"risk_per_trade_pct": 0}}, "risk_per_trade_pct"),
    ({"risk": {"risk_per_trade_pct": 3.0}}, "<= max_daily_loss_pct"),
    ({"risk": {"leverage": 0.5}}, "leverage"),
    ({"risk": {"leverage": 6}}, "leverage"),
    ({"risk": {"margin_buffer": 1.2}}, "margin_buffer"),
    ({"risk": {"max_open_positions": 0}}, "max_open_positions"),
    ({"risk": {"max_per_sector": 4}}, "max_per_sector must be <= max_open_positions"),
    ({"strategy": {"last_entry": "15:15"}}, "last_entry must be before force_exit"),
    ({"news": {"veto_confidence": 101}}, "veto_confidence"),
])
def test_out_of_range_values_are_rejected(tmp_path, sections, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(write(tmp_path, settings_dict(**sections)))


def test_validate_reports_every_problem():
    s = make_settings(risk={"leverage": 0.5, "margin_buffer": 2})
    assert len(validate(s)) == 2


def test_to_dict_is_json_serializable():
    d = to_dict(make_settings())
    assert d["strategy"]["last_entry"] == "14:30"
    assert json.loads(json.dumps(d)) == d
