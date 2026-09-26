"""Каталог инструментов с fallback: REST -> кэш data/instruments_{category}.json -> вывод из потока.

Вывод из потока (source="inferred"): шаг цены = минимальное число знаков после запятой среди
увиденных цен стакана/сделок, шаг лота — среди размеров сделок. Для paper-торговли этого достаточно,
но при первой возможности каталог обновляется с REST и сохраняется в кэш.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from ..types import SymbolMeta
from .rest import BybitRest, BybitRestError

log = logging.getLogger(__name__)


def _decimals_of(x: float) -> int:
    s = repr(float(x))
    if "e" in s or "E" in s:
        s = f"{x:.12f}".rstrip("0")
    return len(s.split(".")[1].rstrip("0")) if "." in s else 0


class InstrumentCatalog:
    def __init__(self, category: str, data_dir: str | Path = "data") -> None:
        self.category = category
        self.data_dir = Path(data_dir)
        self.cache_path = self.data_dir / f"instruments_{category}.json"
        self.metas: dict[str, SymbolMeta] = {}
        self._price_dec: dict[str, int] = {}
        self._qty_dec: dict[str, int] = {}
        self._prices: dict[str, set[float]] = {}
        self.turnover: dict[str, float] = {}

    # ---------------------------------------------------------------- загрузка
    async def load(self, rest: BybitRest | None, symbols: list[str] | None = None, quote_coin: str = "USDT") -> str:
        """Заполнить каталог. Возвращает источник: rest | cache | none."""
        if rest is not None:
            try:
                lst = await rest.instruments(self.category, quote_coin)
                self.metas = {m.symbol: m for m in lst}
                try:
                    tick = await rest.tickers(self.category)
                    for s, t in tick.items():
                        if s in self.metas:
                            self.metas[s].turnover_24h = float(t.get("turnover24h") or 0)
                        self.turnover[s] = float(t.get("turnover24h") or 0)
                except BybitRestError as e:
                    log.warning("tickers недоступны: %s", e)
                self.save_cache()
                return "rest"
            except (BybitRestError, Exception) as e:  # noqa: BLE001
                log.warning("REST instruments недоступен (%s: %s), пробую кэш", type(e).__name__, str(e)[:120])
        if self.cache_path.exists():
            try:
                raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
                self.metas = {d["symbol"]: SymbolMeta.from_dict(d) for d in raw.get("instruments", [])}
                for m in self.metas.values():
                    m.source = "cache"
                return "cache"
            except (OSError, ValueError, KeyError) as e:
                log.warning("кэш инструментов повреждён: %s", e)
        if symbols:
            for s in symbols:
                self.metas.setdefault(s, SymbolMeta(symbol=s, category=self.category, source="default"))
        return "none"

    def save_cache(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({"saved_at": int(time.time()), "instruments": [m.to_dict() for m in self.metas.values()]},
                           ensure_ascii=False, indent=0),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("не удалось сохранить кэш инструментов: %s", e)

    # ---------------------------------------------------------------- доступ
    def get(self, symbol: str) -> SymbolMeta:
        m = self.metas.get(symbol)
        if m is None:
            m = SymbolMeta(symbol=symbol, category=self.category, source="default")
            self.metas[symbol] = m
        return m

    def __contains__(self, symbol: str) -> bool:
        return symbol in self.metas

    def liquid_symbols(self, min_turnover: float, max_symbols: int, quote_coin: str = "USDT") -> list[str]:
        cands = [m for m in self.metas.values() if m.quote_coin == quote_coin and m.turnover_24h >= min_turnover]
        cands.sort(key=lambda m: -m.turnover_24h)
        return [m.symbol for m in cands[: max_symbols or None]]

    # ---------------------------------------------------------------- вывод из потока
    def observe_price(self, symbol: str, *prices: float) -> bool:
        """Уточнить шаг цены по наблюдённым ценам. Возвращает True, если tick_size изменился.

        Знаки после запятой дают верхнюю границу точности (price_scale), а минимальная
        разность между различными ценами — оценку самого шага (у BTCUSDT scale=2, tick=0.1)."""
        m = self.metas.get(symbol)
        if m is None or m.source in ("rest", "cache"):
            return False
        seen = self._prices.setdefault(symbol, set())
        d = self._price_dec.get(symbol, 0)
        for p in prices:
            if p > 0:
                d = max(d, _decimals_of(p))
                if len(seen) < 400:
                    seen.add(round(p, 10))
        old_tick = m.tick_size
        self._price_dec[symbol] = d
        m.price_scale = d
        tick = 10.0 ** (-d)
        if len(seen) >= 20:
            ps = sorted(seen)
            md = min((b - a for a, b in zip(ps, ps[1:]) if b - a > 0), default=tick)
            # шаг — степень десяти или её кратное (0.1, 0.5, 0.25): округляем к 10^-d кратному
            md = round(md, d)
            if md > 0:
                tick = md
        m.tick_size = tick
        m.source = "inferred"
        return abs(tick - old_tick) > 1e-15

    def observe_qty(self, symbol: str, *qtys: float) -> None:
        m = self.metas.get(symbol)
        if m is None or m.source in ("rest", "cache"):
            return
        d = self._qty_dec.get(symbol, 0)
        for q in qtys:
            if q > 0:
                d = max(d, _decimals_of(q))
        if d != self._qty_dec.get(symbol):
            self._qty_dec[symbol] = d
            m.qty_step = 10.0 ** (-d)
            m.min_qty = m.qty_step
            m.source = "inferred"
