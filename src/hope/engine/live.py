"""Live-движок: публичный WebSocket Bybit -> EngineCore (paper) -> SQLite. Часы — системные."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from pathlib import Path

from ..bybit.instruments import InstrumentCatalog
from ..bybit.rest import BybitRest
from ..bybit.ws import BybitPublicWS
from ..config import Config
from ..store.db import Store
from ..types import bybit_kline_interval, now_ms
from .core import EngineCore
from .strategy import Strategy, load_strategy_class

log = logging.getLogger("hope.live")


def git_branch() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


async def run_live(cfg: Config, duration_secs: float | None = None, run_name: str | None = None,
                   warmup_bars: int | None = None) -> dict:
    """warmup_bars: сколько закрытых свечей подгрузить до старта (None = strategy.history_bars, 0 = без прогрева)."""
    if warmup_bars is None:
        warmup_bars = cfg.strategy.history_bars
    data_dir = Path(cfg.run.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    strategy_cls = load_strategy_class(cfg.strategy.class_path)
    strategy: Strategy = strategy_cls(cfg.strategy.params)

    rest = BybitRest(cfg.exchange.rest_url)
    catalog = InstrumentCatalog(cfg.exchange.category, data_dir)
    src = await catalog.load(rest, cfg.exchange.symbols, cfg.exchange.quote_coin)
    log.info("инструменты: источник=%s, всего=%d", src, len(catalog.metas))
    symbols = list(cfg.exchange.symbols)
    if not symbols:
        symbols = catalog.liquid_symbols(cfg.exchange.min_turnover_24h_usd, cfg.exchange.max_symbols, cfg.exchange.quote_coin)
        if not symbols:
            await rest.close()
            raise RuntimeError("список символов пуст: задайте exchange.symbols или откройте доступ к REST для автоотбора")
    log.info("символы (%d): %s", len(symbols), ", ".join(symbols[:20]) + (" ..." if len(symbols) > 20 else ""))
    # прогрев свечей (если стратегии нужна история)
    warm: dict[str, list] = {}
    if warmup_bars > 0:
        from .warmup import warmup_candles

        warm = await warmup_candles(symbols, cfg.strategy.timeframe, warmup_bars, rest if src == "rest" else None,
                                    cfg.exchange.category, data_dir, now_ms())
    await rest.close()

    store = Store(cfg.store.db_path)
    run_id = store.start_run(
        name=run_name or cfg.run.name or strategy.name,
        strategy=strategy.name,
        mode="live",
        initial_equity=cfg.paper.initial_equity_usd,
        symbols=symbols,
        config=cfg.model_dump(by_alias=True),
        params=strategy.params,
        branch=git_branch(),
    )
    log.info("run_id=%d db=%s", run_id, cfg.store.db_path)

    core = EngineCore(cfg, strategy, store, "live", {s: catalog.get(s) for s in symbols}, symbols, use_book=True,
                      catalog=catalog)
    for s, cs in warm.items():
        for c in cs:
            core.sym[s].candles.on_kline(c)
    if warm:
        log.info("прогрев: %s", ", ".join(f"{s}:{len(cs)}" for s, cs in list(warm.items())[:8]) + (" ..." if len(warm) > 8 else ""))
    q: asyncio.Queue = asyncio.Queue(maxsize=100_000)
    ws = BybitPublicWS(cfg.exchange.ws_url, q, cfg.exchange.topics_per_connection, cfg.exchange.args_per_subscribe)
    depth = strategy.orderbook_depth or cfg.exchange.orderbook_depth
    topics: list[str] = []
    for s in symbols:
        topics.append(f"orderbook.{depth}.{s}")
        if cfg.exchange.subscribe_trades and strategy.needs_trades:
            topics.append(f"publicTrade.{s}")
        if cfg.exchange.subscribe_kline or strategy.needs_kline:
            topics.append(f"kline.{bybit_kline_interval(cfg.strategy.timeframe)}.{s}")
        if strategy.needs_tickers:
            topics.append(f"tickers.{s}")
    ws.subscribe(topics)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    sigs = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):  # Windows: Ctrl+Break и закрытие окна консоли
        sigs.append(signal.SIGBREAK)
    prev_handlers: dict = {}
    for sig in sigs:
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Windows: add_signal_handler не поддерживается — обычный обработчик, который будит цикл
            try:
                prev_handlers[sig] = signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
            except (ValueError, OSError):
                pass
    if duration_secs:
        loop.call_later(duration_secs, stop.set)

    ws_task = asyncio.create_task(ws.run(), name="ws")
    status = "finished"
    zombie = False
    core.start(now_ms())
    store.event(core.now, "info", f"старт live: {len(symbols)} символов, {len(topics)} топиков, инструменты={src}")
    tick_every = 0.1
    next_tick = loop.time()
    try:
        while not stop.is_set():
            timeout = max(0.0, next_tick - loop.time())
            try:
                ev = await asyncio.wait_for(q.get(), timeout=timeout)
                core.handle(ev, now_ms())
                # быстро выгребаем накопившееся без ожидания
                for _ in range(2000):
                    try:
                        ev = q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    core.handle(ev, now_ms())
            except asyncio.TimeoutError:
                pass
            if loop.time() >= next_tick:
                core.tick(now_ms())
                next_tick = loop.time() + tick_every
    except asyncio.CancelledError:
        status = "stopped"
        raise
    except Exception as e:  # noqa: BLE001
        status = "crashed"
        log.exception("движок упал: %s", e)
        store.event(now_ms(), "error", f"движок упал: {type(e).__name__}: {e}")
        raise
    finally:
        log.info("остановка: закрываю WebSocket ...")
        ws.stop()
        try:
            await asyncio.wait_for(asyncio.gather(ws_task, return_exceptions=True), timeout=15)
        except asyncio.TimeoutError:
            zombie = True
            log.warning("WebSocket не закрылся за 15 с, продолжаю остановку")
            for t in asyncio.all_tasks():
                if t is asyncio.current_task():
                    continue
                frames = t.get_stack(limit=4)
                where = " <- ".join(f"{f.f_code.co_name}:{f.f_lineno}" for f in frames)
                log.warning("зависшая задача %s done=%s: %s", t.get_name(), t.done(), where)
        for sig, prev in prev_handlers.items():  # вернуть обработчики: иначе они держат закрытый цикл
            try:
                signal.signal(sig, prev)
            except (ValueError, OSError, TypeError):
                pass
        log.info("остановка: стратегия и итоговые снимки ...")
        core.stop(now_ms())
        summary = core.summary()
        summary["ws"] = ws.stats
        store.finish_run(status, summary)
        store.close()
        log.info("остановка: БД закрыта")
        log.info("итог: %s", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in summary.items()})
        if zombie:
            # зависшие сетевые задачи не дадут asyncio.run() завершиться — выходим жёстко, всё уже записано
            logging.shutdown()
            os._exit(0)
    return summary
