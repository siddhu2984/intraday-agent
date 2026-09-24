import pytest

from agent.risk.sizing import size_position
from conftest import make_settings

RISK = make_settings().risk  # 0.5% risk, 20% max position, 1x leverage


def test_position_cap_binds_with_a_tight_stop():
    r = size_position(entry=1000, stop=990, equity=100_000, risk=RISK)
    assert (r.risk_qty, r.cap_qty, r.qty, r.binding) == (50, 20, 20, "position_cap")
    assert r.risk_amount == pytest.approx(200)  # 0.2% at risk, not the configured 0.5%


def test_risk_binds_with_a_wide_stop():
    r = size_position(entry=1000, stop=970, equity=100_000, risk=RISK)
    assert (r.risk_qty, r.cap_qty, r.qty, r.binding) == (16, 20, 16, "risk")
    assert r.risk_amount == pytest.approx(480)


def test_equal_limits_count_as_risk_binding():
    r = size_position(entry=1000, stop=975, equity=100_000, risk=RISK)
    assert (r.risk_qty, r.cap_qty, r.binding) == (20, 20, "risk")


def test_leverage_raises_the_cap():
    risk = make_settings(risk={"leverage": 5}).risk
    r = size_position(entry=1000, stop=990, equity=100_000, risk=risk)
    assert (r.cap_qty, r.qty, r.binding) == (100, 50, "risk")


def test_short_uses_the_distance():
    assert size_position(entry=1000, stop=1010, equity=100_000, risk=RISK).risk_qty == 50


def test_float_rounding_does_not_lose_a_share():
    # 1000 - 999.9 is 0.10000000000002 in floats, so 500 / it is 4999.9999999989
    r = size_position(entry=1000, stop=999.9, equity=100_000, risk=RISK)
    assert r.risk_qty == 5000


def test_expensive_stock_rounds_to_zero():
    r = size_position(entry=25_000, stop=24_900, equity=100_000, risk=RISK)
    assert (r.cap_qty, r.qty) == (0, 0)


@pytest.mark.parametrize("entry, stop, equity", [(1000, 1000, 100_000), (0, 10, 100_000), (1000, 990, 0)])
def test_invalid_inputs_size_to_zero(entry, stop, equity):
    r = size_position(entry=entry, stop=stop, equity=equity, risk=RISK)
    assert (r.qty, r.binding) == (0, "invalid")
