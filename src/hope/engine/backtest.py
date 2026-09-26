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
from ..types import Bbo, BboEvent, Side, SymbolMeta, Trade, TradesEvent, timeframe_ms
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
                       progress: bool = True, mode: str = "trades") -> dict:
    """mode="trades": каждая сделка архива (BBO-прокси, очередь) — для тиковых/мейкерских стратегий.
    mode="candles": свечи таймфрейма стратегии из архива, BBO = close ± полтика — быстро, для свечных стратегий
    с рыночными ордерами (лимитные ордера в этом режиме исполняются только при движении цены сквозь них)."""
    if mode not in ("trades", "candles"):
        raise ValueError("mode должен быть trades или candles")
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
    core.candles_only = mode == "candles"
    proxies = {s: BboProxy(catalog.get(s), queue_ahead_usd) for s in symbols}
    # фандинг в бэктесте: постоянная ставка каждые 8 часов (00/08/16 UTC)
    for s in symbols:
        st = core.sym[s]
        st.funding_rate = funding_rate_8h
        st.next_funding_ts = (start_ts // (8 * 3_600_000) + 1) * 8 * 3_600_000

    if mode == "candles":
        return await _run_candles(cfg, core, store, frames, symbols, catalog, start_ts, run_id, progress)

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


async def _run_candles(cfg: Config, core: EngineCore, store: Store, frames: dict, symbols: list[str], catalog: InstrumentCatalog,
                       start_ts: int, run_id: int, progress: bool) -> dict:
    """Быстрый бэктест по свечам: на закрытии каждой свечи — BBO вокруг close, затем KlineEvent."""
    from ..bybit.history import candles_from_trades
    from ..types import Candle, KlineEvent, bybit_kline_interval

    tf_ms = timeframe_ms(cfg.strategy.timeframe)
    interval = bybit_kline_interval(cfg.strategy.timeframe)
    rows: list[tuple[int, int, str, Candle]] = []
    for i, s in enumerate(symbols):
        cdf = candles_from_trades(frames[s], tf_ms)
        for r in cdf.itertuples(index=False):
            c = Candle(int(r.ts_open), int(r.ts_open) + tf_ms, float(r.open), float(r.high), float(r.low), float(r.close),
                       float(r.volume), float(r.turnover), int(r.n_trades), float(r.buy_volume), closed=True)
            rows.append((c.ts_close, i, s, c))
    rows.sort(key=lambda x: (x[0], x[1]))
    total = len(rows)
    log.info("run_id=%d: режим свечей, %d свечей %s, %d символов", run_id, total, cfg.strategy.timeframe, len(symbols))
    core.start(start_ts)
    t0 = time.time()
    n = 0
    status = "finished"
    try:
        for ts, _, s, c in rows:
            meta = catalog.get(s)
            half = meta.tick_size / 2
            bbo = Bbo(ts=ts, bid=meta.round_price(c.close - half), ask=meta.round_price(c.close + half), bid_qty=1e9, ask_qty=1e9)
            if bbo.bid <= 0 or bbo.ask <= bbo.bid:
                bbo = Bbo(ts=ts, bid=c.close, ask=c.close + meta.tick_size, bid_qty=1e9, ask_qty=1e9)
            core.tick(ts - 1)
            core.handle(BboEvent(s, bbo), ts)
            core.handle(KlineEvent(s, interval, c), ts)
            core.tick(ts + cfg.paper.latency_ms + 1)
            n += 1
            if progress and n % 20_000 == 0:
                log.info("%d/%d свечей (%.0f%%), equity=%.2f, fills=%d", n, total, n / total * 100, core.pf.equity, core.pf.n_fills)
        end_ts = rows[-1][0] + tf_ms if rows else start_ts
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
        summary["candles_processed"] = n
        summary["elapsed_secs"] = round(time.time() - t0, 1)
        store.finish_run(status, summary, finished_ts=core.now)
        store.close()
    log.info("итог: %s", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in summary.items() if k != "instruments"})
    summary["run_id"] = run_id
    return summary
