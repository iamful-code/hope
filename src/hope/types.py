"""Общие типы значений: используются движком, paper-брокером, бэктестом и хранилищем.

Все временные метки — целые миллисекунды Unix (UTC). Цены и количества — float; для
сравнения уровней стакана цена приводится к целым тикам через SymbolMeta.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    BUY = "Buy"
    SELL = "Sell"

    @property
    def sign(self) -> float:
        return 1.0 if self is Side.BUY else -1.0

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY

    @classmethod
    def parse(cls, s: str) -> "Side":
        s = s.lower()
        if s in ("buy", "b", "long", "bid"):
            return cls.BUY
        if s in ("sell", "s", "short", "ask"):
            return cls.SELL
        raise ValueError(f"неизвестная сторона: {s!r}")


class Purpose(str, Enum):
    """Зачем выставлен ордер: помогает аналитике отделять входы от выходов."""

    ENTRY = "entry"
    EXIT = "exit"
    STALE_EXIT = "stale_exit"
    STOP = "stop"
    OTHER = "other"


@dataclass(slots=True)
class SymbolMeta:
    """Статическое описание инструмента (шаги цены и лота) плюс медленно меняющиеся поля."""

    symbol: str
    base_coin: str = ""
    quote_coin: str = "USDT"
    tick_size: float = 0.01
    qty_step: float = 0.001
    min_qty: float = 0.001
    max_qty: float = 1e12
    min_notional: float = 5.0
    price_scale: int = 2
    turnover_24h: float = 0.0
    category: str = "linear"
    # откуда взяты метаданные: rest | cache | inferred (выведены из потока) | default
    source: str = "default"

    def price_to_ticks(self, price: float) -> int:
        return int(round(price / self.tick_size))

    def ticks_to_price(self, ticks: int) -> float:
        # округление до 10 знаков убирает float-шум при любом шаге цены (min тик на Bybit 1e-8)
        return round(ticks * self.tick_size, 10)

    def round_price(self, price: float) -> float:
        return self.ticks_to_price(self.price_to_ticks(price))

    def round_qty_down(self, qty: float) -> float:
        """Округлить количество ВНИЗ до шага лота; 0, если меньше минимального."""
        if self.qty_step <= 0:
            return qty
        steps = int(qty / self.qty_step + 1e-9)
        q = round(steps * self.qty_step, 10)
        if q < self.min_qty - 1e-12:
            return 0.0
        return min(q, self.max_qty)

    def tick_bps(self, price: float) -> float:
        return self.tick_size / price * 1e4 if price > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "base_coin": self.base_coin,
            "quote_coin": self.quote_coin,
            "tick_size": self.tick_size,
            "qty_step": self.qty_step,
            "min_qty": self.min_qty,
            "max_qty": self.max_qty,
            "min_notional": self.min_notional,
            "price_scale": self.price_scale,
            "turnover_24h": self.turnover_24h,
            "category": self.category,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SymbolMeta":
        return cls(**{k: d[k] for k in cls.__slots__ if k in d})  # type: ignore[attr-defined]


@dataclass(slots=True)
class Bbo:
    """Лучшие bid/ask."""

    ts: int = 0
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: float = 0.0
    ask_qty: float = 0.0

    @property
    def valid(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask > self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) * 0.5

    @property
    def spread_bps(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / m * 1e4 if m > 0 else 0.0

    @property
    def imbalance(self) -> float:
        """Дисбаланс объёма на лучших уровнях в [-1, 1] (положительный = больше на bid)."""
        tot = self.bid_qty + self.ask_qty
        return (self.bid_qty - self.ask_qty) / tot if tot > 0 else 0.0

    @property
    def microprice(self) -> float:
        tot = self.bid_qty + self.ask_qty
        if tot <= 0:
            return self.mid
        return (self.bid * self.ask_qty + self.ask * self.bid_qty) / tot


@dataclass(slots=True)
class Trade:
    """Публичная сделка; taker_side — сторона агрессора."""

    ts: int
    price: float
    qty: float
    taker_side: Side


@dataclass(slots=True)
class Candle:
    """Свеча заданного таймфрейма. closed=True, когда интервал завершён."""

    ts_open: int
    ts_close: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    turnover: float = 0.0
    n_trades: int = 0
    buy_volume: float = 0.0
    closed: bool = False

    @property
    def sell_volume(self) -> float:
        return self.volume - self.buy_volume


class OrderState(str, Enum):
    PENDING = "pending"  # отправлен, ещё не дошёл до биржи (латентность)
    ACTIVE = "active"
    CANCEL_PENDING = "cancel_pending"
    DONE = "done"


@dataclass(slots=True)
class Order:
    id: int
    symbol: str
    side: Side
    price: float
    qty: float
    taker: bool = False
    post_only: bool = True
    reduce_only: bool = False
    purpose: Purpose = Purpose.ENTRY
    tag: str = ""
    ts_created: int = 0
    ts_active: int = 0
    cancel_at: int | None = None
    state: OrderState = OrderState.PENDING
    filled: float = 0.0
    queue_ahead: float = 0.0
    queue_ahead_initial: float = 0.0
    mid_at_place: float = 0.0
    spread_bps_at_place: float = 0.0
    inventory_before: float = 0.0
    status: str = ""

    @property
    def remaining(self) -> float:
        return max(self.qty - self.filled, 0.0)

    @property
    def is_live(self) -> bool:
        return self.state in (OrderState.PENDING, OrderState.ACTIVE, OrderState.CANCEL_PENDING)

    @property
    def notional(self) -> float:
        return self.price * self.qty


@dataclass(slots=True)
class Fill:
    order_id: int
    symbol: str
    side: Side
    price: float
    qty: float
    fee: float
    ts: int
    is_maker: bool
    purpose: Purpose
    tag: str = ""
    bid: float = 0.0
    ask: float = 0.0
    placed_ts: int = 0
    mid_at_place: float = 0.0
    spread_bps_at_place: float = 0.0
    queue_ahead_initial: float = 0.0
    inventory_before: float = 0.0
    realized_pnl: float = 0.0  # заполняет портфель
    position_after: float = 0.0  # заполняет портфель

    @property
    def notional(self) -> float:
        return self.price * self.qty


@dataclass(slots=True)
class OrderDone:
    order_id: int
    symbol: str
    side: Side
    price: float
    qty: float
    filled: float
    ts_created: int
    ts_done: int
    status: str
    purpose: Purpose
    tag: str
    taker: bool
    queue_ahead_initial: float
    spread_bps_at_place: float


# ---------------------------------------------------------------- рыночные события


@dataclass(slots=True)
class BookEvent:
    """Обновление стакана (snapshot или delta) с WebSocket."""

    symbol: str
    ts: int
    snapshot: bool
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    seq: int = 0
    update_id: int = 0


@dataclass(slots=True)
class BboEvent:
    symbol: str
    bbo: Bbo


@dataclass(slots=True)
class TradesEvent:
    symbol: str
    trades: list[Trade]


@dataclass(slots=True)
class KlineEvent:
    """Свеча из топика kline.{interval}.{symbol} (интервал в минутах или 'D')."""

    symbol: str
    interval: str
    candle: Candle


@dataclass(slots=True)
class TickerEvent:
    symbol: str
    ts: int
    fields: dict = field(default_factory=dict)  # lastPrice, turnover24h, fundingRate, openInterest ...


@dataclass(slots=True)
class StatusEvent:
    conn: int
    msg: str
    ts: int = 0


MarketEvent = BookEvent | BboEvent | TradesEvent | KlineEvent | TickerEvent | StatusEvent


def now_ms() -> int:
    return int(time.time() * 1000)


TIMEFRAME_MS: dict[str, int] = {
    "1s": 1_000,
    "5s": 5_000,
    "15s": 15_000,
    "30s": 30_000,
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


def timeframe_ms(tf: str) -> int:
    try:
        return TIMEFRAME_MS[tf]
    except KeyError as e:
        raise ValueError(f"неизвестный таймфрейм {tf!r}; допустимы: {', '.join(TIMEFRAME_MS)}") from e


def bybit_kline_interval(tf: str) -> str:
    """Таймфрейм в формате топика kline Bybit: минуты числом, 'D' для дня."""
    ms = timeframe_ms(tf)
    if ms < 60_000:
        raise ValueError(f"топик kline Bybit не поддерживает таймфрейм {tf}")
    if tf == "1d":
        return "D"
    return str(ms // 60_000)
