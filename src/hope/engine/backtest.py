"""Бэктест на архиве сделок Bybit тем же EngineCore и paper-брокером, что и live.

Без стакана в истории BBO оценивается по сделкам: тейкерская покупка по p => ask = p,
тейкерская продажа по p => bid = p (с поддержанием ask > bid). Объём на лучших уровнях неизвестен —
очередь впереди нашего лимитного ордера задаётся параметром backtest.queue_ahead_usd.
Это консервативно для тейкерных стратегий и приблизительно для мейкерских.
"""

from __future__ import annotations

import heapq
import logging
import time
from datetime import date
from pathlib import Path

from ..bybit.history import TradeHistory, utc_day_ms
from ..bybit.instruments import InstrumentCatalog
from ..config import Config
from ..store.db import Store
from ..types import Bbo, BboEvent, Side, SymbolMeta, Trade, TradesEvent
from .core import EngineCore
from .live import git_branch
from .strategy import load_strategy_class

log = logging.getLogger("hope.backtest")


class BboProxy:
    __slots__ = ("meta", "bid", "ask", "qty", "queue_usd")

    def __init__(self, meta: SymbolMeta, queue_usd: float) -> None:
        self.meta = meta
        self.bid = 0.0
        self.ask = 0.0
        self.queue_usd = queue_usd
        self.qty = 0.0

    def on_trade(self, t: Trade) -> Bbo | None:
        tick = self.meta.tick_size
        if t.taker_side is Side.BUY:
            self.ask = t.price
            if self.bid <= 0 or self.bid >= self.ask:
                self.bid = self.meta.round_price(self.ask - tick)
        else:
            self.bid = t.price
            if self.ask <= 0 or self.ask <= self.bid:
                self.ask = self.meta.round_price(self.bid + tick)
        if self.bid <= 0 or self.ask <= 0:
            return None
        q = self.queue_usd / t.price if t.price > 0 else 0.0
        return Bbo(ts=t.ts, bid=self.bid, ask=self.ask, bid_qty=q, ask_qty=q)


async def run_backtest(cfg: Config, from_: str | None = None, to: str | None = None, run_name: str | None = None,
                       queue_ahead_usd: float = 20_000.0, funding_rate_8h: float = 0.0001, store_path: str | None = None,
                       progress: bool = True) -> dict:
    from_ = from_ or cfg.backtest.from_
    to = to or cfg.backtest.to
    symbols = list(cfg.exchange.symbols)
    if not symbols:
        raise ValueError("для бэктеста нужен явный список exchange.symbols")
    data_dir = Path(cfg.run.data_dir)
    strategy = load_strategy_class(cfg.strategy.class_path)(cfg.strategy.params)

    catalog = InstrumentCatalog(cfg.exchange.category, data_dir)
    await catalog.load(None, symbols, cfg.exchange.quote_coin)
    hist = TradeHistory(data_dir, cfg.exchange.category)
    missing = await hist.ensure(symbols, from_, to)
    if missing:
        log.warning("нет данных за %d символ-дней (пропущены): %s", len(missing), missing[:5])

    frames = {}
    for s in symbols:
        df = hist.load(s, from_, to)
        if df.empty:
            log.warning("%s: пустая история, символ исключён", s)
            continue
        frames[s] = df
        # вывод шага цены/лота из истории, если каталог не знает символ
        m = catalog.get(s)
        if m.source in ("default", "inferred"):
            from ..bybit.instruments import _decimals_of

            sample = df["price"].iloc[:5000]
            d = int(max(_decimals_of(float(x)) for x in sample))
            m.tick_size, m.price_scale = 10.0 ** (-d), d
            dq = int(max(_decimals_of(float(x)) for x in df["qty"].iloc[:5000]))
            m.qty_step = m.min_qty = 10.0 ** (-dq)
            m.source = "inferred"
    symbols = [s for s in symbols if s in frames]
    if not symbols:
        raise RuntimeError("нет истории ни по одному символу")

    store = Store(store_path or cfg.store.db_path)
    start_ts = utc_day_ms(date.fromisoformat(from_))
    run_id = store.start_run(
        name=run_name or cfg.run.name or f"{strategy.name} bt {from_}..{to}",
        strategy=strategy.name,
        mode="backtest",
        initial_equity=cfg.paper.initial_equity_usd,
        symbols=symbols,
        config=cfg.model_dump(by_alias=True),
        params=strategy.params,
        branch=git_branch(),
        started_ts=start_ts,
    )
    core = EngineCore(cfg, strategy, store, "backtest", {s: catalog.get(s) for s in symbols}, symbols, use_book=False,
                      taker_slippage_bps=cfg.backtest.taker_slippage_bps)
    proxies = {s: BboProxy(catalog.get(s), queue_ahead_usd) for s in symbols}
    # фандинг в бэктесте: постоянная ставка каждые 8 часов (00/08/16 UTC)
    for s in symbols:
        st = core.sym[s]
        st.funding_rate = funding_rate_8h
        st.next_funding_ts = (start_ts // (8 * 3_600_000) + 1) * 8 * 3_600_000

    # слияние потоков сделок по времени
    iters = {s: TradeHistory.iter_trades(frames[s]) for s in symbols}
    heap: list[tuple[int, int, str, Trade]] = []
    for i, s in enumerate(symbols):
        t = next(iters[s], None)
        if t is not None:
            heap.append((t.ts, i, s, t))
    heapq.heapify(heap)
    total = sum(len(f) for f in frames.values())
    log.info("run_id=%d: %d сделок, %d символов, %s..%s", run_id, total, len(symbols), from_, to)

    core.start(start_ts)
    n = 0
    t0 = time.time()
    tick_ms = 100
    next_tick = start_ts
    status = "finished"
    try:
        while heap:
            ts, i, s, t = heapq.heappop(heap)
            nxt = next(iters[s], None)
            if nxt is not None:
                heapq.heappush(heap, (nxt.ts, i, s, nxt))
            while next_tick <= ts:
                core.tick(next_tick)
                next_tick += tick_ms
            bbo = proxies[s].on_trade(t)
            if bbo is not None:
                core.handle(BboEvent(s, bbo), ts)
            core.handle(TradesEvent(s, [t]), ts)
            n += 1
            if progress and n % 500_000 == 0:
                el = time.time() - t0
                log.info("%d/%d сделок (%.0f%%), %.0f сделок/с, equity=%.2f, fills=%d", n, total, n / total * 100, n / el,
                         core.pf.equity, core.pf.n_fills)
        end_ts = next_tick
        core.tick(end_ts)
        for s in symbols:
            core.close_position(s, taker=True, tag="bt_end")
        core.tick(end_ts + cfg.paper.latency_ms + 1)
    except Exception as e:  # noqa: BLE001
        status = "crashed"
        store.event(core.now, "error", f"бэктест упал: {type(e).__name__}: {e}")
        raise
    finally:
        core.stop(core.now)
        summary = core.summary()
        summary["trades_processed"] = n
        summary["elapsed_secs"] = round(time.time() - t0, 1)
        store.finish_run(status, summary, finished_ts=core.now)
        store.close()
    log.info("итог: %s", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in summary.items()})
    summary["run_id"] = run_id
    return summary
