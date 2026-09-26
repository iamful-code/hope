"""Интерфейс стратегии и контекст исполнения.

Стратегия — класс с обработчиками событий. Один и тот же код работает в live (WebSocket Bybit)
и в бэктесте (архив сделок): различается только источник событий и часы.

    class MyStrategy(Strategy):
        name = "my"
        def on_candle(self, ctx, symbol, candle):
            closes = ctx.candles(symbol).close
            if len(closes) < 20: return
            if closes[-1] > closes[-20:].mean() and ctx.position(symbol).qty == 0:
                ctx.place_market(symbol, Side.BUY, ctx.qty_for_notional(symbol, 100))
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..types import Bbo, Candle, Fill, Order, OrderDone, Purpose, Side, SymbolMeta, Trade

if TYPE_CHECKING:
    from ..market.book import OrderBook
    from ..market.candles import CandleSeries
    from ..market.stats import RollingStats
    from ..paper.portfolio import Portfolio, Position


class Context:
    """API, доступный стратегии. Реализуется движком (EngineCore)."""

    mode: str = "live"
    cfg: Any = None  # hope.config.Config текущего запуска
    params: dict[str, Any] = {}
    symbols: list[str] = []
    now: int = 0
    log: logging.Logger

    # рынок
    def meta(self, symbol: str) -> SymbolMeta: ...
    def bbo(self, symbol: str) -> Bbo | None: ...
    def book(self, symbol: str) -> "OrderBook | None": ...
    def candles(self, symbol: str) -> "CandleSeries": ...
    def current_candle(self, symbol: str) -> Candle | None: ...
    def stats(self, symbol: str) -> "RollingStats": ...
    def ticker(self, symbol: str) -> dict: ...
    # портфель
    @property
    def portfolio(self) -> "Portfolio": ...
    def position(self, symbol: str) -> "Position": ...
    def orders(self, symbol: str | None = None) -> list[Order]: ...
    # ордера
    def place_limit(self, symbol: str, side: Side, price: float, qty: float, *, post_only: bool = True,
                    purpose: Purpose = Purpose.ENTRY, tag: str = "", reduce_only: bool = False) -> Order | None: ...
    def place_market(self, symbol: str, side: Side, qty: float, *, purpose: Purpose = Purpose.ENTRY,
                     tag: str = "", reduce_only: bool = False) -> Order | None: ...
    def cancel(self, order_id: int) -> bool: ...
    def cancel_all(self, symbol: str | None = None) -> int: ...
    def close_position(self, symbol: str, *, taker: bool = True, tag: str = "close") -> Order | None: ...
    # утилиты
    def qty_for_notional(self, symbol: str, notional_usd: float, price: float | None = None) -> float: ...
    def metric(self, name: str, value: float, symbol: str = "") -> None: ...
    def event(self, msg: str, level: str = "info", symbol: str = "") -> None: ...


class Strategy:
    """Базовый класс стратегии. Переопределяйте нужные обработчики."""

    name: str = "strategy"
    # какие подписки нужны движку (live): стакан всегда; сделки нужны для свечей и paper-исполнения
    needs_trades: bool = True
    needs_kline: bool = False
    needs_tickers: bool = True
    orderbook_depth: int | None = None  # None = из конфига

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = dict(self.default_params())
        self.params.update(params or {})

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {}

    def p(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    # ---- жизненный цикл
    def on_start(self, ctx: Context) -> None: ...
    def on_stop(self, ctx: Context) -> None: ...

    # ---- рынок
    def on_bbo(self, ctx: Context, symbol: str, bbo: Bbo) -> None: ...
    def on_trade(self, ctx: Context, symbol: str, trade: Trade) -> None: ...
    def on_candle(self, ctx: Context, symbol: str, candle: Candle) -> None: ...
    def on_ticker(self, ctx: Context, symbol: str, fields: dict) -> None: ...
    def on_timer(self, ctx: Context) -> None: ...

    # ---- исполнение
    def on_fill(self, ctx: Context, fill: Fill) -> None: ...
    def on_order_done(self, ctx: Context, done: OrderDone) -> None: ...


def load_strategy_class(path: str) -> type[Strategy]:
    """'пакет.модуль:Класс' -> класс. Поддерживает также путь к файлу 'strategies/x/strategy.py:Класс'."""
    if ":" not in path:
        raise ValueError("ожидается 'модуль:Класс' или 'путь/к/файлу.py:Класс'")
    mod_s, cls_s = path.rsplit(":", 1)
    if mod_s.endswith(".py"):
        p = Path(mod_s)
        spec = importlib.util.spec_from_file_location(p.stem, p)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[p.stem] = mod
        spec.loader.exec_module(mod)
    else:
        mod = importlib.import_module(mod_s)
    cls = getattr(mod, cls_s)
    if not (isinstance(cls, type) and issubclass(cls, Strategy)):
        raise TypeError(f"{path} не является подклассом Strategy")
    return cls
