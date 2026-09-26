"""Конфигурация: YAML (base + стратегия) + переопределения из окружения, валидация через pydantic."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from .types import timeframe_ms


class RunCfg(BaseModel):
    name: str = ""
    data_dir: str = "data"


class ExchangeCfg(BaseModel):
    category: str = "linear"
    rest_url: str = "https://api.bybit.com"
    ws_url: str = "wss://stream.bybit.com/v5/public/linear"
    # запасные адреса WebSocket при частых обрывах; None — stream.bytick.com (второй домен Bybit), [] — без запасных
    ws_fallback_urls: list[str] | None = None
    # прокси для WebSocket: auto — системный/из окружения (как у браузера), none — напрямую, или URL http://host:port
    ws_proxy: str = "auto"
    orderbook_depth: int = 1
    topics_per_connection: int = 200
    args_per_subscribe: int = 50
    subscribe_trades: bool = True
    subscribe_kline: bool = False
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    quote_coin: str = "USDT"
    min_turnover_24h_usd: float = 5_000_000.0
    max_symbols: int = 50

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        if v not in ("linear", "spot", "inverse"):
            raise ValueError("category должен быть linear | spot | inverse")
        return v


class FeesCfg(BaseModel):
    maker: float = 0.0002
    taker: float = 0.00055


class PaperCfg(BaseModel):
    latency_ms: int = 60
    initial_equity_usd: float = 10_000.0
    taker_slippage_bps: float = 0.0
    apply_funding: bool = True


class RiskCfg(BaseModel):
    max_daily_loss_usd: float = 300.0
    max_gross_notional_usd: float = 5_000.0
    max_position_notional_usd: float = 1_000.0
    max_open_orders_per_symbol: int = 10


class StoreCfg(BaseModel):
    db_path: str = "data/hope.db"
    snapshot_secs: float = 5.0
    record_market: bool = False


class StrategyCfg(BaseModel):
    class_path: str = Field(default="hope.strategies.examples:BboImbalance", alias="class")
    timeframe: str = "1m"
    history_bars: int = 500
    timer_ms: int = 1000
    params: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @field_validator("timeframe")
    @classmethod
    def _tf(cls, v: str) -> str:
        timeframe_ms(v)
        return v


class BacktestCfg(BaseModel):
    from_: str = Field(default="2026-09-01", alias="from")
    to: str = "2026-09-07"
    taker_slippage_bps: float = 1.0

    model_config = {"populate_by_name": True}


class MonitorCfg(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class Config(BaseModel):
    run: RunCfg = Field(default_factory=RunCfg)
    exchange: ExchangeCfg = Field(default_factory=ExchangeCfg)
    fees: FeesCfg = Field(default_factory=FeesCfg)
    paper: PaperCfg = Field(default_factory=PaperCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    store: StoreCfg = Field(default_factory=StoreCfg)
    strategy: StrategyCfg = Field(default_factory=StrategyCfg)
    backtest: BacktestCfg = Field(default_factory=BacktestCfg)
    monitor: MonitorCfg = Field(default_factory=MonitorCfg)

    def resolved_path(self, p: str) -> Path:
        """Относительные пути считаются от data_dir, если они не начинаются с data/ или /."""
        path = Path(p)
        if path.is_absolute():
            return path
        return path


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _coerce(v: str) -> Any:
    """Строка из окружения -> число/bool/список/строка."""
    s = v.strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    if s.startswith("[") or s.startswith("{"):
        return yaml.safe_load(s)
    if "," in s:
        return [x.strip() for x in s.split(",") if x.strip()]
    return s


def env_overrides(prefix: str = "HOPE__") -> dict:
    out: dict = {}
    for k, v in os.environ.items():
        if not k.startswith(prefix):
            continue
        parts = k[len(prefix) :].split("__")
        if not parts or any(not p for p in parts):
            continue
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p.lower(), {})
        cur[parts[-1].lower()] = _coerce(v)
    return out


def load_dotenv(path: str | Path = ".env", override: bool = False) -> dict[str, str]:
    """Прочитать KEY=VALUE из .env (без внешних зависимостей) и выставить в окружение.

    Уже заданные переменные окружения не перезаписываются (override=False): значения из консоли,
    Docker или планировщика задач важнее файла. Возвращает применённые пары."""
    p = Path(path)
    applied: dict[str, str] = {}
    if not p.is_file():
        return applied
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key or (not override and key in os.environ):
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


def default_strategy_config() -> Path | None:
    """Конфиг стратегии по умолчанию: переменная STRATEGY_CONFIG (из окружения или .env)."""
    v = os.environ.get("STRATEGY_CONFIG", "").strip()
    return Path(v) if v else None


def base_config_path() -> Path:
    env = os.environ.get("HOPE_BASE_CONFIG")
    if env:
        return Path(env)
    # репозиторий: <root>/config/base.yaml ; установленный пакет: рядом нет — берём cwd
    for cand in (Path.cwd() / "config" / "base.yaml", Path(__file__).resolve().parents[2] / "config" / "base.yaml"):
        if cand.exists():
            return cand
    return Path.cwd() / "config" / "base.yaml"


def load_config(*paths: str | Path | None, overrides: dict | None = None) -> Config:
    """base.yaml -> файлы стратегии по порядку -> переменные окружения -> явные overrides."""
    merged: dict = {}
    base = base_config_path()
    if base.exists():
        merged = yaml.safe_load(base.read_text(encoding="utf-8")) or {}
    for p in paths:
        if not p:
            continue
        pp = Path(p)
        if not pp.exists():
            raise FileNotFoundError(f"конфиг не найден: {pp}")
        merged = deep_merge(merged, yaml.safe_load(pp.read_text(encoding="utf-8")) or {})
    merged = deep_merge(merged, env_overrides())
    if overrides:
        merged = deep_merge(merged, overrides)
    return Config.model_validate(merged)
