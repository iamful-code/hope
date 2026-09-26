"""Сканер символов без REST: подписка на tickers.* по публичному WebSocket, оценка шага цены
(минимальный наблюдённый спред ask1-bid1 и разности цен), оборота за 24ч и «крупнотиковости».

Список кандидатов берётся из каталога инструментов (кэш/REST), а при его отсутствии — из
листинга архива public.bybit.com/trading/ (все когда-либо торговавшиеся символы; неактивные
просто не пришлют тикер).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

from ..types import TickerEvent
from .history import PUBLIC_BASE
from .ws import BybitPublicWS

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ScanRow:
    symbol: str
    last: float = 0.0
    turnover_24h: float = 0.0
    min_spread: float = float("inf")
    tick_est: float = float("inf")
    n: int = 0
    prices: set = field(default_factory=set)
    funding_rate: float = 0.0
    open_interest_value: float = 0.0

    @property
    def tick_bps(self) -> float:
        t = min(self.tick_est, self.min_spread)
        return t / self.last * 1e4 if self.last > 0 and t < float("inf") else 0.0

    @property
    def spread_bps(self) -> float:
        return self.min_spread / self.last * 1e4 if self.last > 0 and self.min_spread < float("inf") else 0.0


async def archive_symbols(category: str = "linear", quote: str = "USDT") -> list[str]:
    sub = "spot" if category == "spot" else "trading"
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.get(f"{PUBLIC_BASE}/{sub}/")
        r.raise_for_status()
    syms = sorted(set(re.findall(r'href="([A-Z0-9]+)/"', r.text)))
    return [s for s in syms if s.endswith(quote)]


def _min_diff(prices: set[float]) -> float:
    if len(prices) < 2:
        return float("inf")
    ps = sorted(prices)
    best = float("inf")
    for a, b in zip(ps, ps[1:]):
        d = b - a
        if 0 < d < best:
            best = d
    return best


async def scan(symbols: list[str], ws_url: str, duration_secs: float = 30.0, topics_per_connection: int = 300,
               args_per_subscribe: int = 100) -> list[ScanRow]:
    q: asyncio.Queue = asyncio.Queue()
    ws = BybitPublicWS(ws_url, q, topics_per_connection, args_per_subscribe)
    ws.subscribe([f"tickers.{s}" for s in symbols])
    task = asyncio.create_task(ws.run())
    rows: dict[str, ScanRow] = {}
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < duration_secs:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not isinstance(ev, TickerEvent):
                continue
            f = ev.fields
            r = rows.setdefault(ev.symbol, ScanRow(ev.symbol))
            r.n += 1
            lp = f.get("lastPrice")
            if lp:
                r.last = float(lp)
            t24 = f.get("turnover24h")
            if t24:
                r.turnover_24h = float(t24)
            fr = f.get("fundingRate")
            if fr:
                r.funding_rate = float(fr)
            oi = f.get("openInterestValue")
            if oi:
                r.open_interest_value = float(oi)
            b, a = f.get("bid1Price"), f.get("ask1Price")
            if b and a:
                bf, af = float(b), float(a)
                if af > bf > 0:
                    r.min_spread = min(r.min_spread, round(af - bf, 10))
                r.prices.add(bf)
                r.prices.add(af)
            if lp:
                r.prices.add(float(lp))
    finally:
        ws.stop()
        await asyncio.gather(task, return_exceptions=True)
    for r in rows.values():
        r.tick_est = _min_diff(r.prices)
    return sorted(rows.values(), key=lambda r: -r.turnover_24h)
