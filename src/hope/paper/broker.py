"""Paper-брокер: симулирует исполнение наших ордеров на Bybit по публичному потоку.

Модель (перенос spr/core/src/paper.rs):
* ордер становится активным через latency_ms после отправки; отмена тоже действует через
  latency_ms (в промежутке ордер может исполниться);
* при активации лимитного ордера очередь впереди нас = показанный объём на нашей цене
  (0, если мы внутри спреда); если показанный объём позже стал меньше нашей оценки — мы
  продвинулись (отмены впереди нас);
* публичная сделка на нашей цене с нужной стороной тейкера сначала съедает очередь впереди,
  остаток исполняет нас; сделка сквозь нашу цену исполняет остаток полностью;
* движение BBO сквозь нашу цену (best ask <= наш bid) исполняет остаток;
* post-only: ордер, который пересёк бы рынок при активации, отклоняется; без post_only —
  исполняется как тейкер по касанию;
* тейкерный (market) ордер исполняется по касанию противоположной стороны плюс проскальзывание.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..types import Bbo, Fill, Order, OrderDone, OrderState, Purpose, Side, Trade


@dataclass(slots=True)
class PlaceRequest:
    symbol: str
    side: Side
    qty: float
    price: float = 0.0
    taker: bool = False
    post_only: bool = True
    reduce_only: bool = False
    purpose: Purpose = Purpose.ENTRY
    tag: str = ""
    inventory_before: float = 0.0


class PaperBroker:
    def __init__(
        self,
        latency_ms: int = 60,
        maker_fee: float = 0.0002,
        taker_fee: float = 0.00055,
        taker_slippage_bps: float = 0.0,
        qty_eps: float = 1e-9,
    ) -> None:
        self.latency_ms = latency_ms
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.taker_slippage_bps = taker_slippage_bps
        self.qty_eps = qty_eps
        self._orders: dict[int, Order] = {}
        self._by_sym: dict[str, list[int]] = defaultdict(list)
        self._last_bbo: dict[str, Bbo] = {}
        self._next_id = 1
        self.fills: list[Fill] = []
        self.done: list[OrderDone] = []

    # ------------------------------------------------------------ доступ
    def order(self, order_id: int) -> Order | None:
        return self._orders.get(order_id)

    def live_orders(self, symbol: str | None = None) -> list[Order]:
        if symbol is None:
            return [o for o in self._orders.values() if o.is_live]
        return [self._orders[i] for i in self._by_sym.get(symbol, []) if self._orders[i].is_live]

    def n_live(self, symbol: str | None = None) -> int:
        return len(self.live_orders(symbol))

    def bbo(self, symbol: str) -> Bbo | None:
        return self._last_bbo.get(symbol)

    # ------------------------------------------------------------ команды
    def place(self, now: int, req: PlaceRequest) -> Order:
        oid = self._next_id
        self._next_id += 1
        bbo = self._last_bbo.get(req.symbol)
        valid = bbo is not None and bbo.valid
        mid = bbo.mid if valid else req.price
        o = Order(
            id=oid,
            symbol=req.symbol,
            side=req.side,
            price=req.price,
            qty=req.qty,
            taker=req.taker,
            post_only=req.post_only and not req.taker,
            reduce_only=req.reduce_only,
            purpose=req.purpose,
            tag=req.tag,
            ts_created=now,
            ts_active=now + self.latency_ms,
            state=OrderState.PENDING,
            mid_at_place=mid,
            spread_bps_at_place=bbo.spread_bps if valid else 0.0,
            inventory_before=req.inventory_before,
        )
        self._orders[oid] = o
        self._by_sym[req.symbol].append(oid)
        return o

    def cancel(self, now: int, order_id: int) -> bool:
        o = self._orders.get(order_id)
        if o is None or not o.is_live or o.cancel_at is not None:
            return False
        o.cancel_at = now + self.latency_ms
        if o.state == OrderState.ACTIVE:
            o.state = OrderState.CANCEL_PENDING
        return True

    def cancel_all(self, now: int, symbol: str | None = None) -> int:
        n = 0
        for o in self.live_orders(symbol):
            if self.cancel(now, o.id):
                n += 1
        return n

    # ------------------------------------------------------------ внутреннее
    def _finish(self, o: Order, now: int, status: str) -> None:
        o.state = OrderState.DONE
        o.status = status
        self._orders.pop(o.id, None)
        lst = self._by_sym.get(o.symbol)
        if lst:
            try:
                lst.remove(o.id)
            except ValueError:
                pass
        self.done.append(
            OrderDone(
                order_id=o.id,
                symbol=o.symbol,
                side=o.side,
                price=o.price,
                qty=o.qty,
                filled=o.filled,
                ts_created=o.ts_created,
                ts_done=now,
                status=status,
                purpose=o.purpose,
                tag=o.tag,
                taker=o.taker,
                queue_ahead_initial=o.queue_ahead_initial,
                spread_bps_at_place=o.spread_bps_at_place,
            )
        )

    def _emit_fill(self, o: Order, now: int, qty: float, price: float, is_maker: bool) -> bool:
        fee_rate = self.maker_fee if is_maker else self.taker_fee
        o.filled += qty
        bbo = self._last_bbo.get(o.symbol)
        self.fills.append(
            Fill(
                order_id=o.id,
                symbol=o.symbol,
                side=o.side,
                price=price,
                qty=qty,
                fee=qty * price * fee_rate,
                ts=now,
                is_maker=is_maker,
                purpose=o.purpose,
                tag=o.tag,
                bid=bbo.bid if bbo else 0.0,
                ask=bbo.ask if bbo else 0.0,
                placed_ts=o.ts_created,
                mid_at_place=o.mid_at_place,
                spread_bps_at_place=o.spread_bps_at_place,
                queue_ahead_initial=o.queue_ahead_initial,
                inventory_before=o.inventory_before,
            )
        )
        complete = o.remaining <= self.qty_eps
        if complete:
            self._finish(o, now, "filled")
        return complete

    def _taker_price(self, side: Side, bbo: Bbo) -> float:
        px = bbo.ask if side is Side.BUY else bbo.bid
        if self.taker_slippage_bps:
            px *= 1.0 + side.sign * self.taker_slippage_bps / 1e4
        return px

    # ------------------------------------------------------------ время
    def on_time(self, now: int) -> None:
        """Активировать дошедшие до биржи ордера и завершить отмены, чья латентность истекла."""
        for oid in list(self._orders.keys()):
            o = self._orders.get(oid)
            if o is None:
                continue
            if o.cancel_at is not None and o.cancel_at <= now:
                self._finish(o, now, "partial_cancelled" if o.filled > 0 else "cancelled")
                continue
            if o.state == OrderState.PENDING and o.ts_active <= now:
                self._activate(o, now)

    def _activate(self, o: Order, now: int) -> None:
        bbo = self._last_bbo.get(o.symbol)
        if bbo is None or not bbo.valid:
            return  # нет рынка — остаётся pending до появления стакана
        if o.taker:
            o.state = OrderState.ACTIVE
            self._emit_fill(o, now, o.remaining, self._taker_price(o.side, bbo), False)
            return
        crosses = o.price >= bbo.ask if o.side is Side.BUY else o.price <= bbo.bid
        if crosses:
            if o.post_only:
                self._finish(o, now, "rejected_post_only")
                return
            # лимитный ордер, пересекающий рынок: исполняем как тейкер по касанию (не хуже лимита)
            o.state = OrderState.ACTIVE
            px = self._taker_price(o.side, bbo)
            px = min(px, o.price) if o.side is Side.BUY else max(px, o.price)
            self._emit_fill(o, now, o.remaining, px, False)
            return
        if o.side is Side.BUY:
            ahead = bbo.bid_qty if o.price <= bbo.bid else 0.0
        else:
            ahead = bbo.ask_qty if o.price >= bbo.ask else 0.0
        o.state = OrderState.ACTIVE
        o.ts_active = now
        o.queue_ahead = ahead
        o.queue_ahead_initial = ahead

    # ------------------------------------------------------------ рынок
    def on_bbo(self, now: int, symbol: str, bbo: Bbo) -> None:
        self._last_bbo[symbol] = bbo
        if not bbo.valid:
            return
        ids = self._by_sym.get(symbol)
        if not ids:
            return
        for oid in list(ids):
            o = self._orders.get(oid)
            if o is None or o.state == OrderState.PENDING:
                continue
            swept = bbo.ask <= o.price if o.side is Side.BUY else bbo.bid >= o.price
            if swept:
                self._emit_fill(o, now, o.remaining, o.price, True)
                continue
            if o.side is Side.BUY:
                if o.price == bbo.bid:
                    o.queue_ahead = min(o.queue_ahead, bbo.bid_qty)
                elif o.price > bbo.bid:
                    o.queue_ahead = 0.0
            else:
                if o.price == bbo.ask:
                    o.queue_ahead = min(o.queue_ahead, bbo.ask_qty)
                elif o.price < bbo.ask:
                    o.queue_ahead = 0.0

    def on_trade(self, now: int, symbol: str, t: Trade) -> None:
        ids = self._by_sym.get(symbol)
        if not ids:
            return
        for oid in list(ids):
            o = self._orders.get(oid)
            if o is None or o.state == OrderState.PENDING:
                continue
            if o.side is Side.BUY:
                hits = t.taker_side is Side.SELL and t.price <= o.price
                through = t.price < o.price
            else:
                hits = t.taker_side is Side.BUY and t.price >= o.price
                through = t.price > o.price
            if not hits:
                continue
            if through:
                self._emit_fill(o, now, o.remaining, o.price, True)
                continue
            v = t.qty
            if o.queue_ahead > 0:
                eat = min(v, o.queue_ahead)
                o.queue_ahead -= eat
                v -= eat
            if v > self.qty_eps:
                self._emit_fill(o, now, min(v, o.remaining), o.price, True)

    def drain(self) -> tuple[list[Fill], list[OrderDone]]:
        f, d = self.fills, self.done
        self.fills, self.done = [], []
        return f, d
