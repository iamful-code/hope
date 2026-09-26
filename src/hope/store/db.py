"""SQLite-хранилище: отдельный поток-писатель с батчами, движок никогда не ждёт диск."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from pathlib import Path

from ..types import Fill, OrderDone, now_ms

SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA busy_timeout=5000",
)


def _connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    for p in _PRAGMAS:
        con.execute(p)
    return con


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"БД не найдена: {p}")
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


class Store:
    """Очередь SQL-операций -> поток -> батчи в одной транзакции (по времени или по размеру)."""

    def __init__(self, path: str | Path, batch_ms: int = 500, batch_size: int = 2000) -> None:
        self.path = Path(path)
        self.con = _connect(self.path)
        self.con.executescript(SCHEMA)
        self._q: queue.Queue[tuple[str, tuple] | None] = queue.Queue()
        self.batch_ms = batch_ms
        self.batch_size = batch_size
        self.run_id: int | None = None
        self._thread = threading.Thread(target=self._loop, name="hope-store", daemon=True)
        self._stopped = threading.Event()
        self.dropped = 0
        self._thread.start()

    # ---------------------------------------------------------------- запуск
    def start_run(
        self,
        name: str,
        strategy: str,
        mode: str,
        initial_equity: float,
        symbols: list[str],
        config: dict,
        params: dict | None = None,
        engine: str = "hope",
        branch: str = "",
        started_ts: int | None = None,
    ) -> int:
        cur = self.con.execute(
            "INSERT INTO runs(name, strategy, mode, engine, branch, started_ts, initial_equity, symbols, config_json, params_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                name,
                strategy,
                mode,
                engine,
                branch,
                started_ts or now_ms(),
                initial_equity,
                json.dumps(symbols),
                json.dumps(config, default=str, ensure_ascii=False),
                json.dumps(params or {}, default=str, ensure_ascii=False),
            ),
        )
        self.run_id = int(cur.lastrowid)
        return self.run_id

    def finish_run(self, status: str = "finished", summary: dict | None = None, finished_ts: int | None = None) -> None:
        self.flush()
        self.con.execute(
            "UPDATE runs SET status=?, finished_ts=?, summary_json=? WHERE run_id=?",
            (status, finished_ts or now_ms(), json.dumps(summary or {}, default=str, ensure_ascii=False), self.run_id),
        )

    # ---------------------------------------------------------------- запись
    def _put(self, sql: str, args: tuple) -> None:
        self._q.put((sql, args))

    def fill(self, f: Fill, seq: int = 0) -> None:
        self._put(
            "INSERT INTO fills(run_id, seq, order_id, symbol, side, price, qty, fee, ts, is_maker, purpose, tag, bid, ask,"
            " placed_ts, mid_at_place, spread_bps_at_place, queue_ahead_initial, inventory_before, realized_pnl, position_after)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.run_id, seq, f.order_id, f.symbol, f.side.value, f.price, f.qty, f.fee, f.ts, int(f.is_maker),
                f.purpose.value, f.tag, f.bid, f.ask, f.placed_ts, f.mid_at_place, f.spread_bps_at_place,
                f.queue_ahead_initial, f.inventory_before, f.realized_pnl, f.position_after,
            ),
        )

    def order_done(self, d: OrderDone) -> None:
        self._put(
            "INSERT OR REPLACE INTO orders(run_id, order_id, symbol, side, price, qty, filled, taker, purpose, tag,"
            " ts_created, ts_done, status, queue_ahead_initial, spread_bps_at_place) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.run_id, d.order_id, d.symbol, d.side.value, d.price, d.qty, d.filled, int(d.taker), d.purpose.value,
                d.tag, d.ts_created, d.ts_done, d.status, d.queue_ahead_initial, d.spread_bps_at_place,
            ),
        )

    def equity(self, ts: int, snap: dict, n_open_orders: int = 0) -> None:
        self._put(
            "INSERT OR REPLACE INTO equity(run_id, ts, equity, realized_pnl, unrealized_pnl, fees, funding, gross_notional,"
            " n_positions, n_fills, n_open_orders, max_drawdown) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.run_id, ts, snap["equity"], snap["realized_pnl"], snap["unrealized_pnl"], snap["fees"],
                snap.get("funding", 0.0), snap["gross_notional"], snap["n_positions"], snap["n_fills"], n_open_orders,
                snap["max_drawdown"],
            ),
        )

    def position(self, ts: int, p) -> None:
        self._put(
            "INSERT OR REPLACE INTO positions(run_id, symbol, qty, avg_price, mark, unrealized_pnl, realized_pnl, fees,"
            " funding, n_fills, opened_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.run_id, p.symbol, p.qty, p.avg_price, p.mark, p.unrealized_pnl, p.realized_pnl, p.fees, p.funding,
                p.n_fills, p.opened_ts, ts,
            ),
        )

    def symbol_stats(self, ts: int, symbol: str, mid: float, spread_bps: float, spread_med: float, vol: float,
                     tpm: float, turnover_pm: float, imb: float) -> None:
        self._put(
            "INSERT OR REPLACE INTO symbol_stats(run_id, ts, symbol, mid, spread_bps, spread_med_bps, vol_bps,"
            " trades_per_min, turnover_per_min, flow_imbalance) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, ts, symbol, mid, spread_bps, spread_med, vol, tpm, turnover_pm, imb),
        )

    def metric(self, ts: int, name: str, value: float, symbol: str = "") -> None:
        self._put("INSERT INTO metrics(run_id, ts, symbol, name, value) VALUES (?,?,?,?,?)",
                  (self.run_id, ts, symbol, name, float(value)))

    def event(self, ts: int, level: str, msg: str, symbol: str = "") -> None:
        self._put("INSERT INTO events(run_id, ts, level, symbol, msg) VALUES (?,?,?,?,?)",
                  (self.run_id, ts, level, symbol, msg))

    def md_bbo(self, ts: int, symbol: str, bid: float, ask: float, bq: float, aq: float) -> None:
        self._put("INSERT INTO md_bbo(run_id, ts, symbol, bid, ask, bid_qty, ask_qty) VALUES (?,?,?,?,?,?,?)",
                  (self.run_id, ts, symbol, bid, ask, bq, aq))

    def md_trade(self, ts: int, symbol: str, side: str, price: float, qty: float) -> None:
        self._put("INSERT INTO md_trades(run_id, ts, symbol, side, price, qty) VALUES (?,?,?,?,?,?)",
                  (self.run_id, ts, symbol, side, price, qty))

    def markout(self, seq: int, symbol: str, horizon_ms: int, mid: float, markout_bps: float) -> None:
        self._put("INSERT OR REPLACE INTO markouts(run_id, seq, symbol, horizon_ms, mid, markout_bps) VALUES (?,?,?,?,?,?)",
                  (self.run_id, seq, symbol, horizon_ms, mid, markout_bps))

    # ---------------------------------------------------------------- поток
    def _loop(self) -> None:
        pending: list[tuple[str, tuple]] = []
        last = time.monotonic()
        while not self._stopped.is_set():
            timeout = max(0.0, self.batch_ms / 1000 - (time.monotonic() - last))
            try:
                item = self._q.get(timeout=timeout)
            except queue.Empty:
                item = ()
            if item is None:
                self._write(pending)
                pending = []
                break
            if item:
                pending.append(item)
            if pending and (len(pending) >= self.batch_size or time.monotonic() - last >= self.batch_ms / 1000):
                self._write(pending)
                pending = []
                last = time.monotonic()
        # добираем остатки очереди
        rest: list[tuple[str, tuple]] = []
        while True:
            try:
                it = self._q.get_nowait()
            except queue.Empty:
                break
            if it:
                rest.append(it)
        if rest:
            self._write(rest)

    def _write(self, batch: list[tuple[str, tuple]]) -> None:
        if not batch:
            return
        for attempt in range(5):
            try:
                self.con.execute("BEGIN")
                for sql, args in batch:
                    self.con.execute(sql, args)
                self.con.execute("COMMIT")
                return
            except sqlite3.OperationalError:
                try:
                    self.con.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                time.sleep(0.05 * (attempt + 1))
        self.dropped += len(batch)

    def flush(self, timeout: float = 5.0) -> None:
        """Дождаться записи всего, что уже в очереди."""
        deadline = time.monotonic() + timeout
        while not self._q.empty() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(min(self.batch_ms / 1000 + 0.05, max(0.0, deadline - time.monotonic())))

    def close(self) -> None:
        if self._stopped.is_set():
            return
        self._q.put(None)
        self._thread.join(timeout=10)
        self._stopped.set()
        self.con.close()


def open_store(path: str | Path) -> Store:
    return Store(path)
