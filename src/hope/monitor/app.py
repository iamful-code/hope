"""FastAPI-приложение монитора: JSON API поверх БД результатов + одностраничный дашборд.

Запуск: `hope monitor --db data/hope.db[,other.db] --port 8000` -> create_app(db).
Монитор — отдельный процесс, только читает БД (read-only соединение на запрос), никогда не пишет.
Ключ запуска в URL: `<run_id>` для одной БД, `<db_index>:<run_id>` при нескольких БД.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..analytics import downsample, equity_curve, spread_captured_bps, to_jsonable
from .data import DbSet, RunBundle, RunNotFound, df_columns, df_records

STATIC_DIR = Path(__file__).parent / "static"

EQUITY_COLUMNS = [
    "ts", "equity", "peak", "drawdown", "drawdown_pct", "realized_pnl", "unrealized_pnl", "fees", "funding",
    "gross_notional", "n_positions", "n_fills", "n_open_orders", "max_drawdown",
]
FILL_COLUMNS = [
    "seq", "fill_id", "order_id", "ts", "symbol", "side", "price", "qty", "notional", "fee", "is_maker", "purpose",
    "tag", "realized_pnl", "position_after", "bid", "ask", "spread_captured_bps", "spread_bps_at_place",
    "queue_ahead_initial", "placed_ts", "mid_at_place", "inventory_before",
]
COMPARE_KEYS = [
    "net_pnl", "return_pct", "gross_pnl", "fees", "fee_share_of_gross", "n_fills", "n_roundtrips", "trades_per_day",
    "win_rate", "profit_factor", "expectancy_per_trade", "avg_hold_secs", "maker_share", "max_drawdown",
    "max_drawdown_pct", "sharpe", "sortino", "markout_5s_bps", "duration_secs",
]


def _closed(rt: pd.DataFrame) -> pd.DataFrame:
    if len(rt) == 0 or "open" not in rt.columns:
        return rt
    return rt[~rt["open"].astype(bool)]


def _open(rt: pd.DataFrame) -> pd.DataFrame:
    if len(rt) == 0 or "open" not in rt.columns:
        return rt.iloc[0:0]
    return rt[rt["open"].astype(bool)]


def _group_stats(df: pd.DataFrame, keys: list[str]) -> list[dict]:
    """Средний/медианный markout по группам -> список dict."""
    if len(df) == 0:
        return []
    g = df.groupby(keys, dropna=False)["markout_bps"].agg(n="count", avg="mean", median="median", std="std")
    g = g.reset_index()
    return df_records(g)


# ====================================================================== расчёты для эндпоинтов
def equity_payload(b: RunBundle, since: int | None, max_points: int) -> dict:
    eq = b.equity.sort_values("ts", kind="mergesort") if len(b.equity) else b.equity
    n_total = int(len(eq))
    if n_total == 0:
        return {"n_total": 0, "n": 0, "first_ts": None, "last_ts": None, "initial_equity": b.run.get("initial_equity"),
                **{c: [] for c in EQUITY_COLUMNS}}
    curve = equity_curve(eq)  # та же длина и порядок: ts и equity NOT NULL в схеме
    df = eq.copy()
    df["peak"] = curve["peak"].to_numpy()
    df["drawdown"] = curve["drawdown"].to_numpy()
    df["drawdown_pct"] = curve["drawdown_pct"].to_numpy()
    first_ts, last_ts = int(df["ts"].iloc[0]), int(df["ts"].iloc[-1])
    if since is not None:
        df = df[df["ts"] > int(since)]
    df = downsample(df, max_points)
    out = {"n_total": n_total, "n": int(len(df)), "first_ts": first_ts, "last_ts": last_ts,
           "initial_equity": b.run.get("initial_equity")}
    out.update(df_columns(df, EQUITY_COLUMNS))
    return out


def fills_payload(b: RunBundle, limit: int, offset: int, symbol: str | None, order: str) -> dict:
    df = b.fills
    if symbol:
        df = df[df["symbol"] == symbol]
    total = int(len(df))
    if total:
        df = df.sort_values(["seq", "ts"], ascending=(order != "desc"), kind="mergesort")
        df = df.iloc[offset: offset + limit].copy()
        df["notional"] = df["price"] * df["qty"]
        df["spread_captured_bps"] = spread_captured_bps(df)
        cols = [c for c in FILL_COLUMNS if c in df.columns]
        rows = df_records(df[cols])
    else:
        rows = []
    symbols = sorted(b.fills["symbol"].astype(str).unique().tolist()) if len(b.fills) else []
    return {"total": total, "limit": limit, "offset": offset, "symbols": symbols, "rows": rows}


def roundtrips_payload(b: RunBundle, limit: int, offset: int, symbol: str | None) -> dict:
    rt = b.roundtrips
    closed, open_ = _closed(rt), _open(rt)
    if symbol:
        closed = closed[closed["symbol"] == symbol]
        open_ = open_[open_["symbol"] == symbol]
    total = int(len(closed))
    rows = df_records(closed.sort_values(["close_ts", "exit_seq"], ascending=False, kind="mergesort").iloc[offset: offset + limit]) if total else []
    cum: dict = {"close_ts": [], "cum_net_pnl": [], "n": []}
    hist: dict = {"edges": [], "counts": []}
    if total:
        asc = closed.sort_values(["close_ts", "exit_seq"], kind="mergesort")
        c = pd.DataFrame({
            "close_ts": asc["close_ts"].astype("int64").to_numpy(),
            "cum_net_pnl": asc["net_pnl"].astype(float).cumsum().to_numpy(),
            "n": np.arange(1, total + 1),
        })
        cum = df_columns(downsample(c, 2000), ["close_ts", "cum_net_pnl", "n"])
        pnl = asc["net_pnl"].astype(float).to_numpy()
        bins = int(min(40, max(5, total // 5)))
        counts, edges = np.histogram(pnl, bins=bins)
        hist = {"edges": [float(x) for x in edges], "counts": [int(x) for x in counts]}
    return {"total": total, "limit": limit, "offset": offset, "rows": rows, "open": df_records(open_), "cum": cum, "hist": hist}


def orders_payload(dbs: DbSet, key: str, limit: int) -> dict:
    db_index, run_id = dbs.parse_key(key)

    def build() -> dict:
        df = dbs.query_df(db_index, "SELECT * FROM orders WHERE run_id=? ORDER BY ts_done, order_id", (run_id,))
        total = int(len(df))
        if total == 0:
            return {"total": 0, "status_counts": {}, "by_purpose": [], "by_status_purpose": [], "limit_orders": None,
                    "taker_orders": {"n": 0}, "queue_bins": [], "all_rows": []}
        df["taker"] = pd.to_numeric(df["taker"], errors="coerce").fillna(0).astype(int)
        df["fill_share"] = np.where(df["qty"] > 0, df["filled"] / df["qty"], 0.0)
        df["time_to_done_ms"] = df["ts_done"] - df["ts_created"]
        status_counts = {str(k): int(v) for k, v in df["status"].value_counts().items()}
        lim = df[df["taker"] == 0]
        tak = df[df["taker"] == 1]
        limit_orders = None
        if len(lim):
            limit_orders = {
                "n": int(len(lim)),
                "n_filled": int((lim["status"] == "filled").sum()),
                "n_partial": int((lim["status"] == "partial_cancelled").sum()),
                "n_cancelled": int((lim["status"] == "cancelled").sum()),
                "n_rejected": int((lim["status"] == "rejected_post_only").sum()),
                "fill_ratio_count": float((lim["status"] == "filled").mean()),
                "fill_ratio_volume": float(lim["filled"].sum() / lim["qty"].sum()) if lim["qty"].sum() > 0 else None,
                "avg_time_to_done_ms": float(lim["time_to_done_ms"].mean()),
                "median_time_to_done_ms": float(lim["time_to_done_ms"].median()),
                "avg_queue_ahead": float(lim["queue_ahead_initial"].mean()),
                "avg_spread_bps_at_place": float(lim["spread_bps_at_place"].mean()),
            }
        by_purpose = df.groupby("purpose").agg(
            n=("order_id", "count"), n_filled=("status", lambda s: int((s == "filled").sum())),
            qty=("qty", "sum"), filled=("filled", "sum"), n_taker=("taker", "sum"),
        ).reset_index()
        by_purpose["fill_ratio_volume"] = np.where(by_purpose["qty"] > 0, by_purpose["filled"] / by_purpose["qty"], None)
        by_status_purpose = df.groupby(["purpose", "status"]).size().reset_index(name="n")
        queue_bins: list[dict] = []
        q = lim[lim["queue_ahead_initial"].notna()]
        if len(q) >= 5 and q["queue_ahead_initial"].nunique() > 1:
            n_bins = int(min(8, q["queue_ahead_initial"].nunique()))
            try:
                cats = pd.qcut(q["queue_ahead_initial"], n_bins, duplicates="drop")
            except ValueError:
                cats = pd.cut(q["queue_ahead_initial"], n_bins)
            for iv, grp in q.groupby(cats, observed=True):
                queue_bins.append({
                    "lo": float(iv.left), "hi": float(iv.right), "n": int(len(grp)),
                    "fill_ratio_count": float((grp["status"] == "filled").mean()),
                    "filled_share": float(grp["filled"].sum() / grp["qty"].sum()) if grp["qty"].sum() > 0 else None,
                    "avg_time_to_done_ms": float(grp["time_to_done_ms"].mean()),
                })
        return {
            "total": total,
            "status_counts": status_counts,
            "by_purpose": df_records(by_purpose),
            "by_status_purpose": df_records(by_status_purpose),
            "limit_orders": limit_orders,
            "taker_orders": {"n": int(len(tak)), "n_filled": int((tak["status"] == "filled").sum())},
            "queue_bins": queue_bins,
            "all_rows": df,  # DataFrame, режется в rows при ответе
        }

    res = dbs.cached(key, "orders", build)
    out = {k: v for k, v in res.items() if k != "all_rows"}
    df = res["all_rows"]
    rows = df_records(df.sort_values(["ts_done", "order_id"], ascending=False, kind="mergesort").head(limit)) if len(df) else []
    out["rows"] = rows
    out["limit"] = limit
    return out


def symbols_payload(dbs: DbSet, key: str) -> dict:
    db_index, run_id = dbs.parse_key(key)
    b = dbs.bundle(key)
    fills, rt = b.fills, _closed(b.roundtrips)
    stats_rows = dbs.query_rows(
        db_index,
        "SELECT s.* FROM symbol_stats s JOIN (SELECT symbol, MAX(ts) AS mts FROM symbol_stats WHERE run_id=? GROUP BY symbol) m"
        " ON s.symbol = m.symbol AND s.ts = m.mts WHERE s.run_id=?",
        (run_id, run_id),
    )
    stats = {r["symbol"]: {k: v for k, v in r.items() if k not in ("run_id", "symbol")} for r in stats_rows}
    pos_rows = dbs.query_rows(db_index, "SELECT * FROM positions WHERE run_id=?", (run_id,))
    pos = {r["symbol"]: r for r in pos_rows}
    run_pub = dbs.run_public(db_index, b.run)
    symbols: list[str] = []
    for s in list(run_pub.get("symbols") or []) + list(stats) + list(pos) + (fills["symbol"].astype(str).unique().tolist() if len(fills) else []):
        if s not in symbols:
            symbols.append(str(s))
    out_rows = []
    for s in symbols:
        f = fills[fills["symbol"] == s] if len(fills) else fills
        r = rt[rt["symbol"] == s] if len(rt) else rt
        p = pos.get(s)
        n_f = int(len(f))
        realized = float(f["realized_pnl"].sum()) if n_f else 0.0
        fees = float(f["fee"].sum()) if n_f else 0.0
        row = {
            "symbol": s,
            "n_fills": n_f,
            "realized_pnl": realized,
            "fees": fees,
            "net_pnl": realized - fees,
            "turnover": float((f["price"] * f["qty"]).sum()) if n_f else 0.0,
            "maker_share": float((f["is_maker"] != 0).mean()) if n_f else None,
            "n_roundtrips": int(len(r)),
            "rt_net_pnl": float(r["net_pnl"].sum()) if len(r) else 0.0,
            "win_rate": float((r["net_pnl"] > 0).mean()) if len(r) else None,
            "avg_hold_secs": float(r["hold_secs"].mean()) if len(r) else None,
            "position_qty": float(p["qty"]) if p else 0.0,
            "avg_price": float(p["avg_price"]) if p else None,
            "unrealized_pnl": float(p["unrealized_pnl"]) if p else 0.0,
            "funding": float(p.get("funding") or 0.0) if p else 0.0,
            "stats": stats.get(s),
        }
        out_rows.append(row)
    return {"rows": out_rows}


def markouts_payload(b: RunBundle) -> dict:
    mk = b.markouts
    if len(mk) == 0:
        return {"n": 0, "horizons": [], "by_horizon": [], "by_maker": [], "by_purpose": [], "by_side": [], "by_symbol": []}
    f = b.fills[["seq", "is_maker", "purpose", "side"]] if len(b.fills) else pd.DataFrame(columns=["seq", "is_maker", "purpose", "side"])
    j = mk.merge(f, on="seq", how="left")
    j["is_maker"] = pd.to_numeric(j["is_maker"], errors="coerce").fillna(-1).astype(int)
    j["purpose"] = j["purpose"].fillna("?")
    j["side"] = j["side"].fillna("?")
    return {
        "n": int(mk["seq"].nunique()),
        "horizons": sorted(int(h) for h in mk["horizon_ms"].unique()),
        "by_horizon": _group_stats(j, ["horizon_ms"]),
        "by_maker": _group_stats(j, ["horizon_ms", "is_maker"]),
        "by_purpose": _group_stats(j, ["horizon_ms", "purpose"]),
        "by_side": _group_stats(j, ["horizon_ms", "side"]),
        "by_symbol": _group_stats(j, ["horizon_ms", "symbol"]),
    }


def metrics_payload(dbs: DbSet, key: str, name: str | None, symbol: str | None, since: int | None, max_points: int) -> dict:
    db_index, run_id = dbs.parse_key(key)
    if not name:
        rows = dbs.query_rows(
            db_index,
            "SELECT name, symbol, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM metrics WHERE run_id=?"
            " GROUP BY name, symbol ORDER BY name, symbol",
            (run_id,),
        )
        return {"names": rows}
    sql = "SELECT ts, symbol, value FROM metrics WHERE run_id=? AND name=?"
    params: list = [run_id, name]
    if symbol is not None:
        sql += " AND symbol=?"
        params.append(symbol)
    if since is not None:
        sql += " AND ts>?"
        params.append(int(since))
    sql += " ORDER BY ts"
    df = dbs.query_df(db_index, sql, tuple(params))
    series = []
    if len(df):
        for sym, grp in df.groupby("symbol", sort=True):
            g = downsample(grp, max_points)
            series.append({"symbol": str(sym), "n_total": int(len(grp)), **df_columns(g, ["ts", "value"])})
    return {"name": name, "series": series}


def compare_payload(dbs: DbSet, run_ids: str, max_points: int) -> dict:
    runs, errors = [], []
    for raw in [s.strip() for s in run_ids.split(",") if s.strip()]:
        try:
            b = dbs.bundle(raw)
        except (RunNotFound, ValueError):
            errors.append(raw)
            continue
        db_index, _ = dbs.parse_key(raw)
        pub = dbs.run_public(db_index, b.run)
        initial = float(b.run.get("initial_equity") or 0.0)
        curve = equity_curve(b.equity)
        payload = {"ts": [], "t_secs": [], "ret_pct": [], "drawdown_pct": []}
        if len(curve):
            c = curve.copy()
            t0 = int(c["ts"].iloc[0])
            c["t_secs"] = (c["ts"] - t0) / 1000.0
            c["ret_pct"] = (c["equity"] / initial - 1.0) * 100.0 if initial > 0 else 0.0
            c = downsample(c, max_points)
            payload = df_columns(c, ["ts", "t_secs", "ret_pct", "drawdown_pct"])
        runs.append({
            "id": pub["id"], "run_id": pub["run_id"], "name": pub["name"], "mode": pub["mode"], "status": pub["status"],
            "strategy": pub["strategy"], "branch": pub.get("branch"), "started_ts": pub["started_ts"],
            "initial_equity": initial,
            "summary": {k: b.summary.get(k) for k in COMPARE_KEYS},
            "verdict": b.summary.get("verdict"),
            "curve": payload,
        })
    return {"runs": runs, "errors": errors}


# ====================================================================== приложение
def create_app(db: str = "data/hope.db") -> FastAPI:
    """Собрать приложение. db — путь к БД или несколько путей через запятую."""
    dbs = DbSet.from_spec(db)
    app = FastAPI(title="hope monitor", description="Монитор результатов лаборатории стратегий (только чтение БД)")
    app.state.dbs = dbs

    @app.exception_handler(RunNotFound)
    async def _not_found(_req: Request, exc: RunNotFound) -> JSONResponse:
        return JSONResponse({"detail": f"запуск не найден: {exc.args[0] if exc.args else ''}"}, status_code=404)

    @app.exception_handler(FileNotFoundError)
    async def _db_missing(_req: Request, exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    def bundle(run_id: str) -> RunBundle:
        try:
            return dbs.bundle(run_id)
        except ValueError as e:  # некорректный ключ
            raise HTTPException(status_code=404, detail=f"некорректный ключ запуска: {run_id}") from e

    # ---------------------------------------------------------------- страницы
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ---------------------------------------------------------------- API
    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "dbs": [asdict(i) for i in dbs.db_infos()], "now_ts": int(time.time() * 1000)}

    @app.get("/api/runs")
    def runs() -> dict:
        rows, infos = dbs.list_runs()
        return to_jsonable({"runs": rows, "dbs": [asdict(i) for i in infos], "multi": dbs.multi, "now_ts": int(time.time() * 1000)})

    @app.get("/api/runs/{run_id}/summary")
    def run_summary(run_id: str) -> dict:
        b = bundle(run_id)
        db_index, _ = dbs.parse_key(run_id)
        return to_jsonable({"run": dbs.run_public(db_index, b.run), "summary": b.summary})

    @app.get("/api/runs/{run_id}/equity")
    def run_equity(run_id: str, since: int | None = None, max_points: int = Query(2000, ge=10, le=50000)) -> dict:
        return to_jsonable(equity_payload(bundle(run_id), since, max_points))

    @app.get("/api/runs/{run_id}/fills")
    def run_fills(run_id: str, limit: int = Query(100, ge=1, le=5000), offset: int = Query(0, ge=0),
                  symbol: str | None = None, order: str = Query("desc", pattern="^(asc|desc)$")) -> dict:
        return to_jsonable(fills_payload(bundle(run_id), limit, offset, symbol, order))

    @app.get("/api/runs/{run_id}/roundtrips")
    def run_roundtrips(run_id: str, limit: int = Query(500, ge=1, le=10000), offset: int = Query(0, ge=0),
                       symbol: str | None = None) -> dict:
        return to_jsonable(roundtrips_payload(bundle(run_id), limit, offset, symbol))

    @app.get("/api/runs/{run_id}/positions")
    def run_positions(run_id: str) -> dict:
        db_index, rid = dbs.parse_key(run_id)
        dbs.run_row(run_id)
        rows = dbs.query_rows(db_index, "SELECT * FROM positions WHERE run_id=? ORDER BY symbol", (rid,))
        for r in rows:
            q = float(r.get("qty") or 0.0)
            r["side"] = "long" if q > 0 else "short" if q < 0 else "flat"
            r["notional"] = abs(q) * float(r.get("mark") or r.get("avg_price") or 0.0)
            r["net_pnl"] = float(r.get("realized_pnl") or 0) - float(r.get("fees") or 0) + float(r.get("funding") or 0) + float(r.get("unrealized_pnl") or 0)
        return to_jsonable({"rows": rows, "n_open": sum(1 for r in rows if r["side"] != "flat")})

    @app.get("/api/runs/{run_id}/orders")
    def run_orders(run_id: str, limit: int = Query(200, ge=1, le=5000)) -> dict:
        bundle(run_id)
        return to_jsonable(orders_payload(dbs, run_id, limit))

    @app.get("/api/runs/{run_id}/symbols")
    def run_symbols(run_id: str) -> dict:
        bundle(run_id)
        return to_jsonable(symbols_payload(dbs, run_id))

    @app.get("/api/runs/{run_id}/markouts")
    def run_markouts(run_id: str) -> dict:
        return to_jsonable(markouts_payload(bundle(run_id)))

    @app.get("/api/runs/{run_id}/metrics")
    def run_metrics(run_id: str, name: str | None = None, symbol: str | None = None, since: int | None = None,
                    max_points: int = Query(5000, ge=10, le=100000)) -> dict:
        dbs.run_row(run_id)
        return to_jsonable(metrics_payload(dbs, run_id, name, symbol, since, max_points))

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: str, limit: int = Query(200, ge=1, le=5000), level: str | None = None) -> dict:
        db_index, rid = dbs.parse_key(run_id)
        dbs.run_row(run_id)
        sql = "SELECT ts, level, symbol, msg FROM events WHERE run_id=?"
        params: list = [rid]
        if level:
            sql += " AND level=?"
            params.append(level)
        total = dbs.query_one(db_index, sql.replace("SELECT ts, level, symbol, msg", "SELECT COUNT(*) AS n"), tuple(params))
        rows = dbs.query_rows(db_index, sql + " ORDER BY ts DESC, rowid DESC LIMIT ?", tuple(params + [limit]))
        levels = [r["level"] for r in dbs.query_rows(db_index, "SELECT DISTINCT level FROM events WHERE run_id=? ORDER BY level", (rid,))]
        return to_jsonable({"total": int(total["n"]) if total else 0, "rows": rows, "levels": levels})

    @app.get("/api/runs/{run_id}/pnl_by_hour")
    def run_pnl_by_hour(run_id: str) -> dict:
        b = bundle(run_id)
        return to_jsonable({"rows": b.summary.get("pnl_by_hour") or []})

    @app.get("/api/compare")
    def compare(run_ids: str = Query(..., description="ключи запусков через запятую"),
                max_points: int = Query(1000, ge=10, le=20000)) -> dict:
        return to_jsonable(compare_payload(dbs, run_ids, max_points))

    return app
