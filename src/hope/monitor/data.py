"""Слой доступа к данным монитора: read-only соединения с одной или несколькими БД, кэш тяжёлых расчётов.

Монитор никогда не пишет в БД. На каждый запрос открывается отдельное read-only соединение
(sqlite это дёшево), поэтому запись движка в WAL-режиме не блокируется и не блокирует нас.
Тяжёлые расчёты (раунд-трипы, сводка) кэшируются на несколько секунд по ключу запуска.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..analytics import roundtrips as _roundtrips
from ..analytics import summary as _summary
from ..store.db import connect_readonly

CACHE_TTL_RUNNING = 3.0  # с, для запусков со status=running
STALE_SECS = 300.0  # нет снимков equity и событий дольше — live-запуск считается умершим (status=stale)
CACHE_TTL_FINISHED = 60.0  # с, для завершённых (данные уже не меняются)
CACHE_MAX_ITEMS = 64


class RunNotFound(KeyError):
    """Запуск с таким ключом не найден ни в одной БД."""


@dataclass
class DbInfo:
    index: int
    path: str
    ok: bool = True
    error: str = ""


@dataclass
class RunBundle:
    """Всё тяжёлое по одному запуску, посчитанное разом и закэшированное."""

    run: dict
    fills: pd.DataFrame
    equity: pd.DataFrame
    markouts: pd.DataFrame
    roundtrips: pd.DataFrame
    summary: dict
    built_ts: float = field(default_factory=time.monotonic)


class TtlCache:
    """Простейший потокобезопасный TTL-кэш: ключ -> (значение, срок годности)."""

    def __init__(self, max_items: int = CACHE_MAX_ITEMS) -> None:
        self._d: dict[Any, tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self.max_items = max_items

    def get_or_build(self, key: Any, ttl_of: Callable[[Any], float], build: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._d.get(key)
            if hit is not None and hit[1] > now:
                return hit[0]
        value = build()  # строим вне блокировки: параллельные запросы к разным запускам не ждут друг друга
        with self._lock:
            if len(self._d) >= self.max_items:
                # выкидываем просроченные, затем самые старые
                for k in [k for k, (_, exp) in self._d.items() if exp <= now]:
                    self._d.pop(k, None)
                while len(self._d) >= self.max_items:
                    self._d.pop(next(iter(self._d)), None)
            self._d[key] = (value, now + ttl_of(value))
        return value

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


def _json_loads(s: Any, default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


class DbSet:
    """Набор БД результатов. Ключ запуска: `<run_id>` для одной БД, `<db_index>:<run_id>` для нескольких."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = [str(Path(p)) for p in paths if str(p).strip()]
        if not self.paths:
            raise ValueError("не задан путь к БД")
        self.cache = TtlCache()

    @classmethod
    def from_spec(cls, spec: str) -> "DbSet":
        return cls([p.strip() for p in str(spec).split(",") if p.strip()])

    @property
    def multi(self) -> bool:
        return len(self.paths) > 1

    # ---------------------------------------------------------------- ключи
    def key(self, db_index: int, run_id: int) -> str | int:
        return f"{db_index}:{run_id}" if self.multi else int(run_id)

    def parse_key(self, key: str | int) -> tuple[int, int]:
        s = str(key).strip()
        if ":" in s:
            a, _, b = s.partition(":")
            db_index, run_id = int(a), int(b)
        else:
            db_index, run_id = 0, int(s)
        if not 0 <= db_index < len(self.paths):
            raise RunNotFound(key)
        return db_index, run_id

    # ---------------------------------------------------------------- соединения
    def connect(self, db_index: int) -> sqlite3.Connection:
        return connect_readonly(self.paths[db_index])

    def db_infos(self) -> list[DbInfo]:
        out: list[DbInfo] = []
        for i, p in enumerate(self.paths):
            try:
                con = self.connect(i)
                con.execute("SELECT 1 FROM runs LIMIT 1").fetchall()
                con.close()
                out.append(DbInfo(i, p, True, ""))
            except Exception as e:  # noqa: BLE001 — отсутствующая/битая БД не должна валить монитор
                out.append(DbInfo(i, p, False, str(e)))
        return out

    def query_df(self, db_index: int, sql: str, params: tuple = ()) -> pd.DataFrame:
        con = self.connect(db_index)
        try:
            return pd.read_sql_query(sql, con, params=params)
        finally:
            con.close()

    def query_rows(self, db_index: int, sql: str, params: tuple = ()) -> list[dict]:
        con = self.connect(db_index)
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    def query_one(self, db_index: int, sql: str, params: tuple = ()) -> dict | None:
        con = self.connect(db_index)
        try:
            r = con.execute(sql, params).fetchone()
            return dict(r) if r is not None else None
        finally:
            con.close()

    # ---------------------------------------------------------------- запуски
    def run_public(self, db_index: int, r: dict) -> dict:
        """Строка runs -> публичный вид: id-ключ, распакованные JSON-поля, без config_json."""
        d = dict(r)
        d["id"] = self.key(db_index, int(r["run_id"]))
        d["db"] = self.paths[db_index]
        d["db_index"] = db_index
        d["symbols"] = _json_loads(r.get("symbols"), [])
        d["params"] = _json_loads(r.get("params_json"), {})
        d["summary"] = _json_loads(r.get("summary_json"), None)
        d["live"] = (r.get("status") == "running")
        d.pop("config_json", None)
        d.pop("params_json", None)
        d.pop("summary_json", None)
        return d

    def list_runs(self) -> tuple[list[dict], list[DbInfo]]:
        """Все запуски по всем БД с быстрой статистикой (последний снимок equity, число исполнений)."""
        runs: list[dict] = []
        infos = self.db_infos()
        for info in infos:
            if not info.ok:
                continue
            i = info.index
            try:
                rows = self.query_rows(i, "SELECT * FROM runs ORDER BY run_id")
                last_eq = {
                    r["run_id"]: r
                    for r in self.query_rows(
                        i,
                        "SELECT e.run_id, e.ts, e.equity, e.n_fills, e.n_positions, e.max_drawdown, e.realized_pnl,"
                        " e.unrealized_pnl, e.fees FROM equity e"
                        " JOIN (SELECT run_id, MAX(ts) AS mts FROM equity GROUP BY run_id) m"
                        " ON e.run_id = m.run_id AND e.ts = m.mts",
                    )
                }
                n_fills = {
                    r["run_id"]: r["n"] for r in self.query_rows(i, "SELECT run_id, COUNT(*) AS n FROM fills GROUP BY run_id")
                }
                last_ev = {
                    r["run_id"]: r["ts"] for r in self.query_rows(i, "SELECT run_id, MAX(ts) AS ts FROM events GROUP BY run_id")
                }
            except Exception as e:  # noqa: BLE001
                info.ok, info.error = False, str(e)
                continue
            for r in rows:
                d = self.run_public(i, r)
                le = last_eq.get(r["run_id"])
                initial = float(r.get("initial_equity") or 0.0)
                equity = float(le["equity"]) if le else initial
                d["equity"] = equity
                d["net_pnl"] = equity - initial
                d["return_pct"] = (equity - initial) / initial * 100.0 if initial > 0 else None
                d["n_fills"] = int(n_fills.get(r["run_id"], 0))
                d["n_positions"] = int(le["n_positions"]) if le else 0
                d["max_drawdown"] = float(le["max_drawdown"]) if le else 0.0
                d["last_equity_ts"] = int(le["ts"]) if le else None
                # живой запуск без снимков equity и событий дольше STALE_SECS — процесс, скорее всего, умер
                # (SIGKILL, сбой хоста). События пишутся и во время прогрева истории, до первых снимков equity.
                if d["live"] and r.get("mode") == "live":
                    last = max(int(le["ts"]) if le else 0, int(last_ev.get(r["run_id"]) or 0), int(r["started_ts"]))
                    d["last_activity_ts"] = last
                    if time.time() * 1000 - last > STALE_SECS * 1000:
                        d["live"] = False
                        d["status"] = "stale"
                end = r.get("finished_ts") or (le["ts"] if le else None) or r.get("started_ts")
                d["duration_secs"] = max(0.0, (int(end) - int(r["started_ts"])) / 1000.0) if end else 0.0
                runs.append(d)
        runs.sort(key=lambda d: (d.get("started_ts") or 0, d["db_index"], d["run_id"]), reverse=True)
        return runs, infos

    def run_row(self, key: str | int) -> tuple[int, dict]:
        db_index, run_id = self.parse_key(key)
        try:
            r = self.query_one(db_index, "SELECT * FROM runs WHERE run_id=?", (run_id,))
        except FileNotFoundError as e:
            raise RunNotFound(key) from e
        if r is None:
            raise RunNotFound(key)
        return db_index, r

    # ---------------------------------------------------------------- тяжёлые данные с кэшем
    def bundle(self, key: str | int) -> RunBundle:
        db_index, run_id = self.parse_key(key)

        def build() -> RunBundle:
            _, run = self.run_row(key)
            fills = self.query_df(db_index, "SELECT * FROM fills WHERE run_id=? ORDER BY seq, ts, fill_id", (run_id,))
            equity = self.query_df(db_index, "SELECT * FROM equity WHERE run_id=? ORDER BY ts", (run_id,))
            markouts = self.query_df(db_index, "SELECT seq, symbol, horizon_ms, mid, markout_bps FROM markouts WHERE run_id=?", (run_id,))
            rt = _roundtrips(fills, include_open=True)
            summ = _summary(run, equity, fills, rt, markouts)
            return RunBundle(run=run, fills=fills, equity=equity, markouts=markouts, roundtrips=rt, summary=summ)

        def ttl(b: RunBundle) -> float:
            return CACHE_TTL_RUNNING if b.run.get("status") == "running" else CACHE_TTL_FINISHED

        return self.cache.get_or_build(("bundle", db_index, run_id), ttl, build)

    def cached(self, key: str | int, name: str, build: Callable[[], Any], ttl: float | None = None) -> Any:
        """Кэш произвольного производного результата по запуску (тот же TTL, что у bundle)."""
        db_index, run_id = self.parse_key(key)
        if ttl is None:
            b = self.bundle(key)
            ttl = CACHE_TTL_RUNNING if b.run.get("status") == "running" else CACHE_TTL_FINISHED
        return self.cache.get_or_build((name, db_index, run_id), lambda _v: ttl, build)


def df_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> список dict с родными типами (NaN -> None)."""
    if df is None or len(df) == 0:
        return []
    out = df.replace({np.nan: None}).to_dict(orient="records")
    clean: list[dict] = []
    for r in out:
        clean.append({k: (v.item() if hasattr(v, "item") else v) for k, v in r.items()})
    return clean


def df_columns(df: pd.DataFrame, cols: list[str]) -> dict[str, list]:
    """DataFrame -> словарь столбцов (компактная передача рядов на клиент)."""
    out: dict[str, list] = {}
    for c in cols:
        if c not in df.columns:
            continue
        s = df[c]
        if pd.api.types.is_float_dtype(s):
            arr = s.to_numpy(dtype=float)
            out[c] = [None if (x != x) else float(x) for x in arr]
        elif pd.api.types.is_integer_dtype(s) or pd.api.types.is_bool_dtype(s):
            out[c] = [x.item() if hasattr(x, "item") else x for x in s.tolist()]
        else:
            out[c] = [None if (x is None or (isinstance(x, float) and x != x)) else x for x in s.tolist()]
    return out
