"""Тесты монитора: каждый эндпоинт отвечает 200 и осмысленным JSON, включая запуск без исполнений.

Основная часть работает на временной БД, собранной по schema.sql (не зависит от data/).
Отдельный тест дополнительно прогоняет API по реальной data/hope.db, если она есть.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hope.monitor.app import create_app
from hope.store.db import SCHEMA

ENDPOINTS = ["summary", "equity", "fills", "roundtrips", "positions", "orders", "symbols", "markouts", "metrics", "events",
             "pnl_by_hour"]


def _make_db(path: Path) -> None:
    """run 1 — live без исполнений; run 2 — завершённый с исполнениями (long, short, переворот, частичные)."""
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    cfg = json.dumps({"x": 1})
    con.execute(
        "INSERT INTO runs(run_id, name, strategy, mode, engine, branch, started_ts, finished_ts, status, initial_equity, symbols,"
        " config_json, params_json, summary_json) VALUES (1,'пустой','tick','live','hope','HEAD',1000,NULL,'running',1000.0,"
        " '[\"AAAUSDT\"]',?, '{}', NULL)", (cfg,))
    con.execute(
        "INSERT INTO runs(run_id, name, strategy, mode, engine, branch, started_ts, finished_ts, status, initial_equity, symbols,"
        " config_json, params_json, summary_json) VALUES (2,'с данными','ema','backtest','hope','HEAD',0,86400000,'finished',"
        " 1000.0,'[\"X\",\"Y\"]',?, '{\"fast\": 9}', '{\"net_pnl\": 1}')", (cfg,))
    # run 1: пара снимков equity и статистика — исполнений нет
    for ts in (1000, 6000):
        con.execute("INSERT INTO equity VALUES (1,?,1000.0,0,0,0,0,0,0,0,0,0)", (ts,))
    con.execute("INSERT INTO symbol_stats VALUES (1,6000,'AAAUSDT',1.0,2.0,1.5,10.0,100.0,5000.0,0.1)")
    con.execute("INSERT INTO events VALUES (1,1000,'info','','старт')")
    # run 2: исполнения
    fills = [
        (1, 1, "X", "Buy", 100.0, 1.0, 0.10, 1000, 1, "entry", "e1"),
        (2, 2, "X", "Sell", 110.0, 0.4, 0.05, 2000, 0, "exit", "x1"),
        (3, 3, "X", "Sell", 90.0, 0.6, 0.06, 3000, 1, "exit", "x1"),
        (4, 4, "X", "Sell", 100.0, 2.0, 0.20, 4000, 1, "entry", "s1"),
        (5, 5, "X", "Buy", 95.0, 3.0, 0.30, 5000, 0, "entry", "flip"),
        (6, 6, "X", "Sell", 105.0, 1.0, 0.10, 8000, 1, "exit", "x2"),
        (7, 7, "Y", "Sell", 50.0, 1.0, 0.01, 1500, 1, "entry", "ys"),
        (8, 8, "Y", "Buy", 52.0, 1.0, 0.01, 2500, 1, "exit", "yx"),
        (9, 9, "X", "Buy", 100.0, 0.5, 0.05, 9000, 1, "entry", "open"),
    ]
    for seq, oid, sym, side, price, qty, fee, ts, maker, purpose, tag in fills:
        con.execute(
            "INSERT INTO fills(run_id, seq, order_id, symbol, side, price, qty, fee, ts, is_maker, purpose, tag, bid, ask,"
            " realized_pnl, position_after) VALUES (2,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)",
            (seq, oid, sym, side, price, qty, fee, ts, maker, purpose, tag, price - 0.5, price + 0.5))
        con.execute(
            "INSERT INTO orders VALUES (2,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, sym, side, price, qty, qty, 1 - maker, purpose, tag, ts - 100, ts, "filled", 10.0 * oid, 1.0))
        for h in (1000, 5000, 30000, 60000):
            con.execute("INSERT INTO markouts VALUES (2,?,?,?,?,?)", (seq, sym, h, price, 0.1 * h / 1000 * (1 if maker else -1)))
    for oid in range(20, 40):
        con.execute("INSERT INTO orders VALUES (2,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (oid, "X", "Buy", 99.0, 1.0, 0.0, 0, "entry", "e", 10_000 + oid, 20_000 + oid, "cancelled", float(oid), 1.0))
    for i, eq in enumerate([1000.0, 1010.0, 990.0, 1020.0, 1015.0]):
        con.execute("INSERT INTO equity VALUES (2,?,?,0,0,0,0.5,0,0,0,0,0)", (i * 300_000, eq))
    con.execute("INSERT INTO positions VALUES (2,'X',0.5,100.0,101.0,0.5,0,0.05,0,7,9000,9000)")
    con.execute("INSERT INTO symbol_stats VALUES (2,9000,'X',100.0,2.0,1.5,10.0,100.0,5000.0,0.1)")
    for i in range(50):
        con.execute("INSERT INTO metrics VALUES (2,?,'X','ema_fast',?)", (i * 1000, 100.0 + i))
        con.execute("INSERT INTO metrics VALUES (2,?,'','state',?)", (i * 1000, i % 3))
    con.execute("INSERT INTO events VALUES (2,1000,'info','','старт')")
    con.execute("INSERT INTO events VALUES (2,2000,'warning','X','что-то')")
    con.commit()
    con.close()


@pytest.fixture(scope="module")
def db_path(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("mon") / "t.db"
    _make_db(p)
    return p


@pytest.fixture(scope="module")
def client(db_path) -> TestClient:
    return TestClient(create_app(str(db_path)))


def _ok(client, path, **params):
    r = client.get(path, params=params)
    assert r.status_code == 200, (path, r.text[:300])
    return r.json()


def test_index_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and "plotly-2.35.2" in r.text and "Обзор" in r.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_runs_list(client):
    j = _ok(client, "/api/runs")
    assert j["multi"] is False and j["dbs"][0]["ok"] is True
    runs = {r["id"]: r for r in j["runs"]}
    assert set(runs) == {1, 2}
    # запуск со status=running, но последний снимок equity в 1970 году -> монитор считает его умершим (stale)
    assert runs[1]["live"] is False and runs[1]["status"] == "stale"
    assert runs[1]["n_fills"] == 0 and runs[1]["last_equity_ts"] == 6000
    assert runs[2]["status"] == "finished" and runs[2]["n_fills"] == 9 and runs[2]["net_pnl"] == pytest.approx(15.0)
    assert runs[2]["params"] == {"fast": 9} and runs[2]["symbols"] == ["X", "Y"]
    assert "config_json" not in runs[2]


@pytest.mark.parametrize("run_id", [1, 2])
@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_every_endpoint_200(client, run_id, endpoint):
    j = _ok(client, f"/api/runs/{run_id}/{endpoint}")
    assert isinstance(j, dict)
    json.dumps(j, allow_nan=False)  # без NaN/Infinity


def test_summary_no_fills(client):
    j = _ok(client, "/api/runs/1/summary")
    s = j["summary"]
    assert s["n_fills"] == 0 and s["n_roundtrips"] == 0 and s["net_pnl"] == 0.0
    assert s["win_rate"] is None and s["profit_factor"] is None and s["sharpe"] is None
    assert not s["verdict"]["checks"]["profitable_after_fees"]["ok"] and s["verdict"]["n_ok"] <= 1
    assert s["duration_secs"] == pytest.approx(5.0)  # до последнего снимка equity (запуск ещё идёт)
    assert j["run"]["live"] is True


def test_summary_with_fills(client):
    s = _ok(client, "/api/runs/2/summary")["summary"]
    assert s["n_fills"] == 9 and s["n_roundtrips"] == 5 and s["n_open_lots"] == 1
    assert s["net_pnl"] == pytest.approx(15.0) and s["return_pct"] == pytest.approx(1.5)
    assert s["max_drawdown"] == pytest.approx(20.0)
    assert s["pnl_by_symbol"]["Y"] == pytest.approx(-2.02)
    assert s["markout_bps"]["5000"] is not None


def test_equity_downsample_and_since(client):
    j = _ok(client, "/api/runs/2/equity", max_points=10)
    assert j["n_total"] == 5 and j["n"] == 5 and j["drawdown"][2] == pytest.approx(20.0)
    j2 = _ok(client, "/api/runs/2/equity", since=300_000)
    assert j2["n"] == 3 and j2["ts"][0] == 600_000
    assert j2["peak"][0] == pytest.approx(1010.0)  # пик учитывает историю до since
    e = _ok(client, "/api/runs/1/equity")
    assert e["n"] == 2 and e["equity"] == [1000.0, 1000.0]


def test_fills_pagination(client):
    j = _ok(client, "/api/runs/2/fills", limit=4, offset=0)
    assert j["total"] == 9 and len(j["rows"]) == 4 and j["rows"][0]["seq"] == 9  # новые сверху
    assert "notional" in j["rows"][0] and "spread_captured_bps" in j["rows"][0]
    j = _ok(client, "/api/runs/2/fills", limit=4, offset=8)
    assert len(j["rows"]) == 1 and j["rows"][0]["seq"] == 1
    j = _ok(client, "/api/runs/2/fills", symbol="Y", order="asc")
    assert j["total"] == 2 and [r["seq"] for r in j["rows"]] == [7, 8]
    assert j["symbols"] == ["X", "Y"]
    e = _ok(client, "/api/runs/1/fills")
    assert e["total"] == 0 and e["rows"] == [] and e["symbols"] == []


def test_roundtrips_endpoint(client):
    j = _ok(client, "/api/runs/2/roundtrips", limit=3)
    assert j["total"] == 5 and len(j["rows"]) == 3 and len(j["open"]) == 1
    assert j["rows"][0]["close_ts"] == 8000  # новые сверху
    assert len(j["cum"]["close_ts"]) == 5 and j["cum"]["cum_net_pnl"][-1] == pytest.approx(3.91 - 6.12 + 9.6 + 9.8 - 2.02)
    assert len(j["hist"]["counts"]) == len(j["hist"]["edges"]) - 1
    e = _ok(client, "/api/runs/1/roundtrips")
    assert e["total"] == 0 and e["cum"]["close_ts"] == [] and e["hist"]["counts"] == []


def test_orders_endpoint(client):
    j = _ok(client, "/api/runs/2/orders", limit=5)
    assert j["total"] == 29 and j["status_counts"] == {"cancelled": 20, "filled": 9}
    lo = j["limit_orders"]
    assert lo["n"] == 27 and lo["n_filled"] == 7 and lo["fill_ratio_count"] == pytest.approx(7 / 27)
    assert j["taker_orders"]["n"] == 2 and len(j["rows"]) == 5
    assert j["queue_bins"] and all("fill_ratio_count" in b for b in j["queue_bins"])
    assert {r["purpose"] for r in j["by_purpose"]} == {"entry", "exit"}
    e = _ok(client, "/api/runs/1/orders")
    assert e["total"] == 0 and e["rows"] == [] and e["limit_orders"] is None


def test_symbols_endpoint(client):
    j = _ok(client, "/api/runs/2/symbols")
    rows = {r["symbol"]: r for r in j["rows"]}
    assert set(rows) == {"X", "Y"}
    assert rows["X"]["n_fills"] == 7 and rows["X"]["position_qty"] == 0.5 and rows["X"]["stats"]["spread_bps"] == 2.0
    assert rows["Y"]["n_roundtrips"] == 1 and rows["Y"]["rt_net_pnl"] == pytest.approx(-2.02) and rows["Y"]["stats"] is None
    e = _ok(client, "/api/runs/1/symbols")
    assert e["rows"][0]["symbol"] == "AAAUSDT" and e["rows"][0]["n_fills"] == 0 and e["rows"][0]["stats"]["mid"] == 1.0


def test_markouts_endpoint(client):
    j = _ok(client, "/api/runs/2/markouts")
    assert j["horizons"] == [1000, 5000, 30000, 60000] and j["n"] == 9
    assert {r["is_maker"] for r in j["by_maker"]} == {0, 1}
    assert {r["purpose"] for r in j["by_purpose"]} == {"entry", "exit"}
    e = _ok(client, "/api/runs/1/markouts")
    assert e["n"] == 0 and e["by_horizon"] == []


def test_metrics_endpoint(client):
    names = _ok(client, "/api/runs/2/metrics")["names"]
    assert {(n["name"], n["symbol"]) for n in names} == {("ema_fast", "X"), ("state", "")}
    j = _ok(client, "/api/runs/2/metrics", name="ema_fast", max_points=10)
    assert len(j["series"]) == 1 and j["series"][0]["n_total"] == 50 and len(j["series"][0]["ts"]) <= 11
    j = _ok(client, "/api/runs/2/metrics", name="ema_fast", since=40_000)
    assert len(j["series"][0]["ts"]) == 9
    assert _ok(client, "/api/runs/2/metrics", name="nope")["series"] == []
    assert _ok(client, "/api/runs/1/metrics")["names"] == []


def test_events_endpoint(client):
    j = _ok(client, "/api/runs/2/events", limit=1)
    assert j["total"] == 2 and len(j["rows"]) == 1 and j["rows"][0]["level"] == "warning"
    assert j["levels"] == ["info", "warning"]
    assert _ok(client, "/api/runs/2/events", level="info")["total"] == 1


def test_positions_and_hours(client):
    j = _ok(client, "/api/runs/2/positions")
    assert j["n_open"] == 1 and j["rows"][0]["side"] == "long" and j["rows"][0]["notional"] == pytest.approx(50.5)
    assert _ok(client, "/api/runs/1/positions") == {"rows": [], "n_open": 0}
    h = _ok(client, "/api/runs/2/pnl_by_hour")["rows"]
    assert len(h) == 24 and sum(x["n"] for x in h) == 5


def test_compare(client):
    j = _ok(client, "/api/compare", run_ids="1,2,99")
    assert [r["id"] for r in j["runs"]] == [1, 2] and j["errors"] == ["99"]
    r2 = j["runs"][1]
    assert r2["curve"]["ret_pct"][-1] == pytest.approx(1.5) and r2["curve"]["t_secs"][-1] == pytest.approx(1200.0)
    assert r2["summary"]["net_pnl"] == pytest.approx(15.0) and r2["verdict"]["n_total"] == 7
    assert j["runs"][0]["curve"]["ret_pct"] == [0.0, 0.0]


def test_not_found(client):
    assert client.get("/api/runs/99/summary").status_code == 404
    assert client.get("/api/runs/abc/equity").status_code == 404
    assert client.get("/api/runs/99/events").status_code == 404
    assert client.get("/api/health").json()["ok"] is True


def test_multi_db(db_path, tmp_path):
    other = tmp_path / "o.db"
    _make_db(other)
    c = TestClient(create_app(f"{db_path},{other},{tmp_path / 'missing.db'}"))
    j = _ok(c, "/api/runs")
    assert j["multi"] is True and [d["ok"] for d in j["dbs"]] == [True, True, False]
    assert sorted(r["id"] for r in j["runs"]) == ["0:1", "0:2", "1:1", "1:2"]
    assert _ok(c, "/api/runs/1:2/summary")["summary"]["n_roundtrips"] == 5
    assert _ok(c, "/api/runs/2/fills")["total"] == 9  # без префикса — первая БД
    cmp = _ok(c, "/api/compare", run_ids="0:2,1:2")
    assert [r["id"] for r in cmp["runs"]] == ["0:2", "1:2"]
    assert c.get("/api/runs/2:1/summary").status_code == 404


REAL_DB = Path(__file__).resolve().parent.parent / "data" / "hope.db"


@pytest.mark.skipif(not REAL_DB.exists(), reason="нет data/hope.db")
def test_real_db_all_endpoints():
    c = TestClient(create_app(str(REAL_DB)))
    runs = _ok(c, "/api/runs")["runs"]
    assert runs
    for r in runs[:6]:
        for ep in ENDPOINTS:
            j = _ok(c, f"/api/runs/{r['id']}/{ep}")
            json.dumps(j, allow_nan=False)
    _ok(c, "/api/compare", run_ids=",".join(str(r["id"]) for r in runs[:3]))
