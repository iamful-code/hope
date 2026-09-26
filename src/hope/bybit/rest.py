"""REST Bybit V5 (только публичные эндпоинты): инструменты, тикеры, свечи. Работает без ключей.

Примечание: с IP из некоторых стран (например, США) Bybit отвечает 403 (CloudFront). Тогда
InstrumentCatalog переходит на кэш или вывод метаданных из потока.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from ..types import Candle, SymbolMeta

log = logging.getLogger(__name__)


class BybitRestError(RuntimeError):
    pass


class BybitRest:
    def __init__(self, base_url: str = "https://api.bybit.com", timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        verify: Any = os.environ.get("SSL_CERT_FILE") or True
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, verify=verify)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict) -> dict:
        r = await self._client.get(path, params=params)
        if r.status_code != 200:
            raise BybitRestError(f"HTTP {r.status_code} {path}: {r.text[:200]}")
        j = r.json()
        if j.get("retCode") != 0:
            raise BybitRestError(f"retCode={j.get('retCode')} {j.get('retMsg')} ({path})")
        return j["result"]

    async def instruments(self, category: str = "linear", quote_coin: str | None = "USDT") -> list[SymbolMeta]:
        out: list[SymbolMeta] = []
        cursor = ""
        while True:
            params: dict = {"category": category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            res = await self._get("/v5/market/instruments-info", params)
            for it in res.get("list", []):
                if it.get("status") != "Trading":
                    continue
                if category == "linear" and it.get("contractType") not in (None, "LinearPerpetual"):
                    continue
                if quote_coin and it.get("quoteCoin") != quote_coin:
                    continue
                pf = it.get("priceFilter", {})
                lf = it.get("lotSizeFilter", {})
                tick = float(pf.get("tickSize", "0.01"))
                step = float(lf.get("qtyStep") or lf.get("basePrecision") or "0.001")
                out.append(
                    SymbolMeta(
                        symbol=it["symbol"],
                        base_coin=it.get("baseCoin", ""),
                        quote_coin=it.get("quoteCoin", ""),
                        tick_size=tick,
                        qty_step=step,
                        min_qty=float(lf.get("minOrderQty", step)),
                        max_qty=float(lf.get("maxOrderQty", 1e12)),
                        min_notional=float(lf.get("minNotionalValue", 5.0) or 5.0),
                        price_scale=int(it.get("priceScale", _decimals(tick))),
                        category=category,
                        source="rest",
                    )
                )
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
        return out

    async def tickers(self, category: str = "linear") -> dict[str, dict]:
        res = await self._get("/v5/market/tickers", {"category": category})
        return {it["symbol"]: it for it in res.get("list", [])}

    async def klines(self, symbol: str, interval: str, start_ms: int, end_ms: int, category: str = "linear") -> list[Candle]:
        """Свечи [start, end) с пагинацией (Bybit отдаёт до 1000 за запрос, новые первыми)."""
        out: list[Candle] = []
        end = end_ms
        interval_ms = (86_400_000 if interval == "D" else int(interval) * 60_000)
        while end > start_ms:
            res = await self._get(
                "/v5/market/kline",
                {"category": category, "symbol": symbol, "interval": interval, "start": start_ms, "end": end - 1, "limit": 1000},
            )
            rows = res.get("list", [])
            if not rows:
                break
            for r in rows:
                ts = int(r[0])
                out.append(Candle(ts, ts + interval_ms, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[6]), closed=True))
            oldest = int(rows[-1][0])
            if oldest <= start_ms or len(rows) < 2:
                break
            end = oldest
        out.sort(key=lambda c: c.ts_open)
        return [c for c in out if start_ms <= c.ts_open < end_ms]


def _decimals(x: float) -> int:
    s = f"{x:.12f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0
