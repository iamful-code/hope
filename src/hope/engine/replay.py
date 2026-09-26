"""Реплей записанных рыночных данных (таблицы md_bbo / md_trades запуска с store.record_market=true)
тем же EngineCore. В отличие от бэктеста на архиве сделок здесь есть настоящие BBO с объёмами на
лучших уровнях, поэтому очередная модель paper-брокера работает как в live — это основной способ
проверять мейкерские стратегии на истории.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from ..bybit.instruments import InstrumentCatalog, _decimals_of
from ..config import Config
from ..store.db import Store, connect_readonly
from ..types import Bbo, BboEvent, Side, Trade, TradesEvent
from .core import EngineCore
from .live import git_branch
from .strategy import load_strategy_class

log = logging.getLogger("hope.replay")


def _source_run(con: sqlite3.Connection, run_id: int) -> dict:
    row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"запуск {run_id} не найден")
    return dict(row)


async def run_replay(cfg: Config, source_db: str, source_run_id: int, run_name: str | None = None,
                     store_path: str | None = None, symbols: list[str] | None = None, speed_log_every: int = 500_000) -> dict:
    src = connect_readonly(source_db)
    srow = _source_run(src, source_run_id)
    n_bbo = src.execute("SELECT count(*) FROM md_bbo WHERE run_id=?", (source_run_id,)).fetchone()[0]
    n_trd = src.execute("SELECT count(*) FROM md_trades WHERE run_id=?", (source_run_id,)).fetchone()[0]
    if n_bbo == 0:
        raise ValueError("в запуске нет записанных рыночных данных (store.record_market=false?)")
    rec_symbols = [r[0] for r in src.execute("SELECT DISTINCT symbol FROM md_bbo WHERE run_id=?", (source_run_id,))]
    symbols = [s for s in (symbols or cfg.exchange.symbols or rec_symbols) if s in rec_symbols]
    if not symbols:
        raise ValueError("нет пересечения между запрошенными символами и записанными")
    # метаданные инструментов: из конфига источника (summary.instruments) или каталог
    catalog = InstrumentCatalog(cfg.exchange.category, Path(cfg.run.data_dir))
    await catalog.load(None, symbols, cfg.exchange.quote_coin)
    try:
        inst = json.loads(srow.get("summary_json") or "{}").get("instruments") or {}
    except ValueError:
        inst = {}
    for s in symbols:
        m = catalog.get(s)
        if m.source in ("default", "inferred") and s in inst:
            m.tick_size = float(inst[s]["tick_size"])
            m.qty_step = float(inst[s]["qty_step"])
            m.min_qty = m.qty_step
            m.price_scale = int(inst[s].get("price_scale") or _decimals_of(m.tick_size))
            m.min_notional = float(inst[s].get("min_notional") or m.min_notional)
            m.source = "inferred"

    strategy = load_strategy_class(cfg.strategy.class_path)(cfg.strategy.params)
    store = Store(store_path or cfg.store.db_path)
    started = int(srow["started_ts"])
    run_id = store.start_run(
        name=run_name or f"{strategy.name} replay run{source_run_id}",
        strategy=strategy.name, mode="replay", initial_equity=cfg.paper.initial_equity_usd, symbols=symbols,
        config=cfg.model_dump(by_alias=True), params=strategy.params, branch=git_branch(), started_ts=started,
    )
    cfg_rec = cfg.model_copy(deep=True)
    cfg_rec.store.record_market = False
    core = EngineCore(cfg_rec, strategy, store, "replay", {s: catalog.get(s) for s in symbols}, symbols, use_book=False)
    log.info("run_id=%d: реплей запуска %d (%s): %d BBO, %d сделок, %d символов", run_id, source_run_id, srow["name"], n_bbo, n_trd, len(symbols))

    ph = ",".join("?" * len(symbols))
    cur_b = src.execute(f"SELECT ts, symbol, bid, ask, bid_qty, ask_qty FROM md_bbo WHERE run_id=? AND symbol IN ({ph}) ORDER BY ts", (source_run_id, *symbols))
    cur_t = src.execute(f"SELECT ts, symbol, side, price, qty FROM md_trades WHERE run_id=? AND symbol IN ({ph}) ORDER BY ts", (source_run_id, *symbols))
    nb = cur_b.fetchone()
    nt = cur_t.fetchone()
    core.start(min(r["ts"] for r in (nb, nt) if r is not None))
    tick_ms = 100
    next_tick = core.now
    n = 0
    t0 = time.time()
    status = "finished"
    try:
        while nb is not None or nt is not None:
            take_bbo = nt is None or (nb is not None and nb["ts"] <= nt["ts"])
            row = nb if take_bbo else nt
            ts = int(row["ts"])
            while next_tick <= ts:
                core.tick(next_tick)
                next_tick += tick_ms
            if take_bbo:
                core.handle(BboEvent(row["symbol"], Bbo(ts, row["bid"], row["ask"], row["bid_qty"], row["ask_qty"])), ts)
                nb = cur_b.fetchone()
            else:
                core.handle(TradesEvent(row["symbol"], [Trade(ts, row["price"], row["qty"], Side.BUY if row["side"] == "Buy" else Side.SELL)]), ts)
                nt = cur_t.fetchone()
            n += 1
            if speed_log_every and n % speed_log_every == 0:
                log.info("%d событий, %.0f/с, equity=%.2f, fills=%d", n, n / (time.time() - t0), core.pf.equity, core.pf.n_fills)
        end_ts = next_tick
        core.tick(end_ts)
        for s in symbols:
            core.close_position(s, taker=True, tag="replay_end")
        core.tick(end_ts + cfg.paper.latency_ms + 1)
    except Exception as e:  # noqa: BLE001
        status = "crashed"
        store.event(core.now, "error", f"реплей упал: {type(e).__name__}: {e}")
        raise
    finally:
        core.stop(core.now)
        summary = core.summary()
        summary["events_processed"] = n
        summary["elapsed_secs"] = round(time.time() - t0, 1)
        summary["source"] = {"db": source_db, "run_id": source_run_id}
        store.finish_run(status, summary, finished_ts=core.now)
        store.close()
        src.close()
    log.info("итог: %s", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in summary.items() if k != "instruments"})
    summary["run_id"] = run_id
    return summary
