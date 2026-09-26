"""Прогрев истории свечей перед live-запуском: REST kline Bybit, а при его недоступности — архив сделок
public.bybit.com за предыдущие дни (доступен без гео-ограничений, но отстаёт до суток: между концом архива
и «сейчас» остаётся разрыв, который стратегия видит как один скачок)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..bybit.history import TradeHistory, candles_from_trades
from ..bybit.rest import BybitRest, BybitRestError
from ..types import Candle, bybit_kline_interval, timeframe_ms

log = logging.getLogger("hope.warmup")


async def warmup_candles(symbols: list[str], timeframe: str, bars: int, rest: BybitRest | None, category: str,
                         data_dir: str | Path, now_ms: int, notify=None) -> dict[str, list[Candle]]:
    """Вернуть до `bars` закрытых свечей на символ (по возрастанию времени)."""
    tf_ms = timeframe_ms(timeframe)
    out: dict[str, list[Candle]] = {}
    end = now_ms - now_ms % tf_ms  # начало текущей (незакрытой) свечи
    start = end - bars * tf_ms
    if rest is not None:
        try:
            interval = bybit_kline_interval(timeframe)
            for s in symbols:
                cs = await rest.klines(s, interval, start, end, category)
                out[s] = [c for c in cs if c.ts_close <= end]
            log.info("прогрев с REST: %d символов, до %d свечей %s", len(out), bars, timeframe)
            return out
        except (BybitRestError, Exception) as e:  # noqa: BLE001
            log.warning("REST kline недоступен (%s: %s), прогрев из архива сделок", type(e).__name__, str(e)[:100])
    # архив: дни, покрывающие [start, вчера]
    hist = TradeHistory(data_dir, category)
    d_from = datetime.fromtimestamp(start / 1000, tz=timezone.utc).date()
    d_to = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date() - timedelta(days=1)
    if d_to < d_from:
        # окно прогрева короче суток, а архив за сегодня ещё не опубликован: берём хвост вчерашнего дня
        d_from = d_to
        start = 0
    todo = hist.pending(symbols, d_from, d_to)
    if todo:
        msg = (f"прогрев: REST Bybit недоступен, качаю архив сделок public.bybit.com — {len(todo)} файлов "
               f"({len(symbols)} символов × {len(todo) // max(len(symbols), 1) or 1} дн., обычно 10–80 МБ каждый). "
               f"Первый раз это может занять несколько минут, дальше файлы берутся из data/history. "
               f"Запуск без прогрева: --warmup 0 (стратегия начнёт торговать, когда накопит свечи вживую)")
        log.warning(msg)
        if notify:
            notify(msg)

    def _progress(done: int, total: int) -> None:
        # в БД — каждый файл (монитор видит, что запуск жив), в консоль — каждые ~10 %
        if notify:
            notify(f"прогрев: скачано {done} из {total} файлов архива")
        if done == total or done % max(total // 10, 1) == 0:
            log.info("прогрев: скачано %d из %d файлов архива", done, total)

    try:
        missing = await hist.ensure(symbols, d_from, d_to, progress=_progress)
    except Exception as e:  # noqa: BLE001
        log.warning("архив недоступен: %s", e)
        return out
    if missing:
        log.info("в архиве нет %d символ-дней", len(missing))
    for s in symbols:
        df = hist.load(s, d_from, d_to)
        if df.empty:
            continue
        cdf = candles_from_trades(df, tf_ms)
        cs = [
            Candle(int(r.ts_open), int(r.ts_open) + tf_ms, float(r.open), float(r.high), float(r.low), float(r.close),
                   float(r.volume), float(r.turnover), int(r.n_trades), float(r.buy_volume), closed=True)
            for r in cdf.itertuples(index=False)
        ]
        cs = [c for c in cs if start <= c.ts_open and c.ts_close <= end][-bars:]
        out[s] = cs
    gap_h = (now_ms - (max((c.ts_close for cs in out.values() for c in cs), default=now_ms))) / 3.6e6
    log.info("прогрев из архива: %d символов, разрыв до текущего момента ~%.1f ч", len(out), gap_h)
    return out
