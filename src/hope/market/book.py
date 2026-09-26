"""Локальный стакан по snapshot/delta Bybit. Уровни хранятся по цене в тиках (int) — без float-шума."""

from __future__ import annotations

from ..types import Bbo, BookEvent, SymbolMeta


class OrderBook:
    __slots__ = ("meta", "bids", "asks", "ts", "update_id", "seq", "_best_bid", "_best_ask", "dirty")

    def __init__(self, meta: SymbolMeta) -> None:
        self.meta = meta
        self.bids: dict[int, float] = {}
        self.asks: dict[int, float] = {}
        self.ts = 0
        self.update_id = 0
        self.seq = 0
        self._best_bid: int | None = None
        self._best_ask: int | None = None
        self.dirty = False

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self._best_bid = self._best_ask = None
        self.dirty = False

    def apply(self, ev: BookEvent) -> Bbo | None:
        """Применить событие. Возвращает новое BBO, если оно изменилось (или snapshot), иначе None."""
        if ev.snapshot or ev.update_id == 1:
            self.clear()
        self.ts = ev.ts
        self.update_id = ev.update_id
        self.seq = ev.seq
        p2t = self.meta.price_to_ticks
        for price, qty in ev.bids:
            t = p2t(price)
            if qty <= 0:
                self.bids.pop(t, None)
            else:
                self.bids[t] = qty
        for price, qty in ev.asks:
            t = p2t(price)
            if qty <= 0:
                self.asks.pop(t, None)
            else:
                self.asks[t] = qty
        bb = max(self.bids) if self.bids else None
        ba = min(self.asks) if self.asks else None
        # пересечённый стакан (устаревшие уровни) — убираем виновников
        while bb is not None and ba is not None and bb >= ba:
            if len(self.bids) >= len(self.asks):
                self.bids.pop(bb)
                bb = max(self.bids) if self.bids else None
            else:
                self.asks.pop(ba)
                ba = min(self.asks) if self.asks else None
        changed = ev.snapshot or bb != self._best_bid or ba != self._best_ask or self.dirty
        self._best_bid, self._best_ask = bb, ba
        self.dirty = False
        if bb is None or ba is None:
            return None
        # BBO меняется и при изменении объёма на лучших уровнях — отдаём всегда, флаг changed для экономии
        return self.bbo()

    def bbo(self) -> Bbo | None:
        if self._best_bid is None or self._best_ask is None:
            return None
        t2p = self.meta.ticks_to_price
        return Bbo(
            ts=self.ts,
            bid=t2p(self._best_bid),
            ask=t2p(self._best_ask),
            bid_qty=self.bids[self._best_bid],
            ask_qty=self.asks[self._best_ask],
        )

    def depth(self, n: int = 10) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        t2p = self.meta.ticks_to_price
        bids = [(t2p(t), self.bids[t]) for t in sorted(self.bids, reverse=True)[:n]]
        asks = [(t2p(t), self.asks[t]) for t in sorted(self.asks)[:n]]
        return bids, asks

    def imbalance(self, levels: int = 5) -> float:
        """Дисбаланс объёма на N лучших уровнях в [-1, 1]."""
        bids, asks = self.depth(levels)
        b = sum(q for _, q in bids)
        a = sum(q for _, q in asks)
        return (b - a) / (b + a) if b + a > 0 else 0.0
