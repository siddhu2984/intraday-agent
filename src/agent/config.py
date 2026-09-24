"""Settings from config/settings.yaml (architecture.md §8).

Unknown and missing keys are errors, so a typo in the YAML fails at startup instead of silently using a default.
"""

from __future__ import annotations

import dataclasses
import typing
from dataclasses import dataclass
from datetime import time
from pathlib import Path

import yaml

from agent.broker.fyers_auth import PROJECT_ROOT

DEFAULT_PATH = PROJECT_ROOT / "config" / "settings.yaml"
MODES = ("backtest", "paper", "live")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float
    max_position_pct: float
    leverage: float
    max_daily_loss_pct: float
    max_consecutive_losses: int
    max_trades_per_day: int
    max_open_positions: int
    max_per_sector: int
    margin_buffer: float
    circuit_buffer_pct: float


@dataclass(frozen=True)
class ScreenerConfig:
    universe: str
    min_price: float
    min_turnover_cr: float
    min_rvol: float
    min_atr_pct: float
    max_atr_pct: float
    watchlist_size: int


@dataclass(frozen=True)
class RescanConfig:
    enabled: bool
    rescan_start: time
    rescan_end: time
    rescan_interval_min: int
    rescan_top_n: int
    rescan_min_rvol: float
    rescan_max_move_pct: float
    rescan_max_vwap_atr: float
    rescan_ttl_min: int
    max_watchlist_size: int
    vwap_touch_pct: float


@dataclass(frozen=True)
class StrategyConfig:
    opening_range_min: int
    entry_buffer_pct: float
    entry_timeout_s: int
    min_stop_pct: float
    max_stop_atr: float
    target_r: float
    breakeven_at_r: float
    last_entry: time
    force_exit: time


@dataclass(frozen=True)
class NewsConfig:
    enabled: bool
    veto_confidence: int
    model: str
    prompt_version: str


@dataclass(frozen=True)
class OpsConfig:
    stale_tick_seconds: int
    max_tick_jump_pct: float
    reconcile_interval_s: int
    max_clock_drift_s: float


@dataclass(frozen=True)
class Settings:
    mode: str
    capital: float
    risk: RiskConfig
    screener: ScreenerConfig
    rescan: RescanConfig
    strategy: StrategyConfig
    news: NewsConfig
    ops: OpsConfig


def _convert(value, kind, where: str):
    if dataclasses.is_dataclass(kind):
        return _build(kind, value, where)
    if kind is time:
        if isinstance(value, str):
            try:
                return time.fromisoformat(value)
            except ValueError:
                pass
        raise ConfigError(f"{where}: expected a time like \"14:30\", got {value!r}")
    if kind is bool:
        if isinstance(value, bool):
            return value
    elif kind is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    elif kind is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif kind is str:
        if isinstance(value, str):
            return value
    raise ConfigError(f"{where}: expected {kind.__name__}, got {value!r}")


def _build(cls, data, where: str):
    if not isinstance(data, dict):
        raise ConfigError(f"{where or 'settings'}: expected a mapping, got {data!r}")
    hints = typing.get_type_hints(cls)
    names = [f.name for f in dataclasses.fields(cls)]
    prefix = f"{where}." if where else ""
    unknown = sorted(set(data) - set(names))
    missing = [n for n in names if n not in data]
    if unknown or missing:
        problems = [f"unknown key {prefix}{k}" for k in unknown] + [f"missing key {prefix}{k}" for k in missing]
        raise ConfigError("; ".join(problems))
    return cls(**{n: _convert(data[n], hints[n], prefix + n) for n in names})


def validate(s: Settings) -> list[str]:
    """Every out-of-range value, as messages (empty if the settings are usable)."""
    problems = []

    def need(ok: bool, message: str) -> None:
        if not ok:
            problems.append(message)

    r, st = s.risk, s.strategy
    need(s.mode in MODES, f"mode must be one of {MODES}")
    need(s.capital > 0, "capital must be > 0")
    for name in ("risk_per_trade_pct", "max_position_pct", "max_daily_loss_pct", "circuit_buffer_pct"):
        need(0 < getattr(r, name) <= 100, f"risk.{name} must be in (0, 100]")
    need(r.risk_per_trade_pct <= r.max_daily_loss_pct, "risk.risk_per_trade_pct must be <= max_daily_loss_pct")
    need(1 <= r.leverage <= 5, "risk.leverage must be in [1, 5]")
    need(0 < r.margin_buffer <= 1, "risk.margin_buffer must be in (0, 1]")
    for name in ("max_consecutive_losses", "max_trades_per_day", "max_open_positions", "max_per_sector"):
        need(getattr(r, name) >= 1, f"risk.{name} must be >= 1")
    need(r.max_per_sector <= r.max_open_positions, "risk.max_per_sector must be <= max_open_positions")
    need(st.opening_range_min >= 1, "strategy.opening_range_min must be >= 1")
    need(st.min_stop_pct > 0, "strategy.min_stop_pct must be > 0")
    need(st.target_r > 0 and st.breakeven_at_r > 0, "strategy.target_r and breakeven_at_r must be > 0")
    need(st.last_entry < st.force_exit, "strategy.last_entry must be before force_exit")
    need(0 <= s.news.veto_confidence <= 100, "news.veto_confidence must be in [0, 100]")
    return problems


def load_settings(path: Path = DEFAULT_PATH) -> Settings:
    with open(path, encoding="utf-8") as f:
        settings = _build(Settings, yaml.safe_load(f), "")
    problems = validate(settings)
    if problems:
        raise ConfigError(f"{path}: " + "; ".join(problems))
    return settings


def to_dict(s: Settings) -> dict:
    """JSON-friendly copy (times as "HH:MM"), e.g. to store with a journal run."""
    def plain(value):
        return value.strftime("%H:%M") if isinstance(value, time) else value
    return {k: ({kk: plain(vv) for kk, vv in v.items()} if isinstance(v, dict) else plain(v))
            for k, v in dataclasses.asdict(s).items()}
