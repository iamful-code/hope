"""Построение свечей из потока сделок (любой таймфрейм) и хранение истории в numpy-массивах.

Одна и та же логика используется в live (WebSocket publicTrade) и в бэктесте (история сделок),
поэтому стратегия видит одинаковые свечи в обоих режимах.
"""

from __future__ import annotations

import numpy as np

from ..types import Candle, Side, Trade, timeframe_ms


class CandleSeries:
    """Кольцевой буфер закрытых свечей с доступом к колонкам как numpy-массивам."""

    COLS = ("ts_open", "open", "high", "low", "close", "volume", "turnover", "n_trades", "buy_volume")

    def __init__(self, capacity: int = 500) -> None:
        self.capacity = capacity
        self._buf = np.zeros((capacity, len(self.COLS)), dtype=np.float64)
        self._n = 0
        self._head = 0  # индекс следующей записи

    def __len__(self) -> int:
        return self._n

    def append(self, c: Candle) -> None:
        self._buf[self._head] = (
            c.ts_open, c.open, c.high, c.low, c.close, c.volume, c.turnover, c.n_trades, c.buy_volume,
        )
        self._head = (self._head + 1) % self.capacity
        self._n = min(self._n + 1, self.capacity)

    def _ordered(self) -> np.ndarray:
        if self._n < self.capacity:
            return self._buf[: self._n]
        return np.concatenate((self._buf[self._head :], self._buf[: self._head]))

    def column(self, name: str, n: int | None = None) -> np.ndarray:
        arr = self._ordered()[:, self.COLS.index(name)]
        return arr if n is None else arr[-n:]

    def last(self, n: int = 1) -> np.ndarray:
        """Последние n свечей как массив (n, 9) в порядке COLS."""
        return self._ordered()[-n:]

    def to_frame(self, n: int | None = None):
        import pandas as pd

        arr = self._ordered() if n is None else self._ordered()[-n:]
        df = pd.DataFrame(arr, columns=list(self.COLS))
        df["ts_open"] = df["ts_open"].astype("int64")
        df["n_trades"] = df["n_trades"].astype("int64")
        df["date"] = pd.to_datetime(df["ts_open"], unit="ms", utc=True)
        return df

    @property
    def close(self) -> np.ndarray:
        return self.column("close")

    @property
    def open(self) -> np.ndarray:
        return self.column("open")

    @property
    def high(self) -> np.ndarray:
        return self.column("high")

    @property
    def low(self) -> np.ndarray:
        return self.column("low")

    @property
    def volume(self) -> np.ndarray:
        return self.column("volume")


class CandleBuilder:
    """Собирает свечи таймфрейма из сделок. Закрытая свеча отдаётся при первой сделке следующего
    интервала или по таймеру (on_time), если интервал истёк без сделок."""

    def __init__(self, timeframe: str, capacity: int = 500) -> None:
        self.tf_ms = timeframe_ms(timeframe)
        self.timeframe = timeframe
        self.current: Candle | None = None
        self.series = CandleSeries(capacity)
        self.last_price = 0.0

    def _bucket(self, ts: int) -> int:
        return ts - ts % self.tf_ms

    def _close_current(self) -> Candle:
        c = self.current
        assert c is not None
        c.closed = True
        self.series.append(c)
        self.current = None
        return c

    def on_trade(self, t: Trade) -> Candle | None:
        """Возвращает закрытую свечу, если эта сделка открыла новый интервал."""
        closed = None
        b = self._bucket(t.ts)
        c = self.current
        if c is not None and b > c.ts_open:
            closed = self._close_current()
            # пустые интервалы между свечами: заполняем плоскими свечами по последней цене
            nxt = closed.ts_open + self.tf_ms
            while nxt < b:
                self.series.append(
                    Candle(nxt, nxt + self.tf_ms, closed.close, closed.close, closed.close, closed.close, closed=True)
                )
                nxt += self.tf_ms
            c = None
        if c is None:
            c = Candle(b, b + self.tf_ms, t.price, t.price, t.price, t.price)
            self.current = c
        if t.price > c.high:
            c.high = t.price
        if t.price < c.low:
            c.low = t.price
        c.close = t.price
        c.volume += t.qty
        c.turnover += t.qty * t.price
        c.n_trades += 1
        if t.taker_side is Side.BUY:
            c.buy_volume += t.qty
        self.last_price = t.price
        return closed

    def on_time(self, now: int) -> Candle | None:
        """Закрыть текущую свечу, если её интервал уже истёк, а сделок нет."""
        c = self.current
        if c is not None and now >= c.ts_close:
            return self._close_current()
        return None

    def on_kline(self, c: Candle) -> Candle | None:
        """Принять готовую свечу (топик kline или история свечей). Закрытые добавляются в серию."""
        if c.closed:
            if len(self.series) == 0 or c.ts_open > self.series.column("ts_open")[-1]:
                self.series.append(c)
                self.current = None
                self.last_price = c.close
                return c
            return None
        self.current = c
        self.last_price = c.close
        return None
