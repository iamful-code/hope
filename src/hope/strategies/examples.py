"""Примеры стратегий для проверки платформы. Не претендуют на прибыльность."""

from __future__ import annotations

import numpy as np

from ..engine.strategy import Context, Strategy
from ..types import Bbo, Candle, Fill, Purpose, Side


class EmaCross(Strategy):
    """Пересечение двух EMA на закрытых свечах, вход/выход рыночными ордерами, long и short."""

    name = "ema_cross"

    @classmethod
    def default_params(cls):
        return {"fast": 9, "slow": 21, "notional_usd": 200.0, "allow_short": True}

    def on_start(self, ctx: Context) -> None:
        self.fast = int(self.p("fast"))
        self.slow = int(self.p("slow"))
        ctx.event(f"старт ema_cross fast={self.fast} slow={self.slow}")

    @staticmethod
    def _ema(x: np.ndarray, n: int) -> float:
        k = 2.0 / (n + 1)
        e = x[0]
        for v in x[1:]:
            e = v * k + e * (1 - k)
        return float(e)

    def on_candle(self, ctx: Context, symbol: str, candle: Candle) -> None:
        closes = ctx.candles(symbol).close
        if len(closes) < self.slow + 2:
            return
        window = closes[-(self.slow * 3) :]
        f_now, s_now = self._ema(window, self.fast), self._ema(window, self.slow)
        f_prev, s_prev = self._ema(window[:-1], self.fast), self._ema(window[:-1], self.slow)
        ctx.metric("ema_fast", f_now, symbol)
        ctx.metric("ema_slow", s_now, symbol)
        pos = ctx.position(symbol).qty
        qty = ctx.qty_for_notional(symbol, float(self.p("notional_usd")))
        if f_prev <= s_prev and f_now > s_now:
            if pos < 0:
                ctx.place_market(symbol, Side.BUY, abs(pos), purpose=Purpose.EXIT, tag="x_short")
            if pos <= 0 and qty > 0:
                ctx.place_market(symbol, Side.BUY, qty, purpose=Purpose.ENTRY, tag="long")
        elif f_prev >= s_prev and f_now < s_now:
            if pos > 0:
                ctx.place_market(symbol, Side.SELL, pos, purpose=Purpose.EXIT, tag="x_long")
            if pos >= 0 and qty > 0 and self.p("allow_short"):
                ctx.place_market(symbol, Side.SELL, qty, purpose=Purpose.ENTRY, tag="short")


class BboImbalance(Strategy):
    """Тиковая стратегия: при сильном дисбалансе объёма на лучших уровнях ставим лимитный ордер
    в сторону дисбаланса на своей стороне книги (мейкер), выходим лимитом на противоположной
    стороне; по таймауту — рыночным. Много сделок, проверка мейкерской модели исполнения."""

    name = "bbo_imbalance"

    @classmethod
    def default_params(cls):
        return {
            "threshold": 0.6,       # |дисбаланс| на лучших уровнях для входа
            "notional_usd": 100.0,
            "max_hold_ms": 30_000,  # держим не дольше
            "min_requote_ms": 500,
            "take_profit_ticks": 2,
        }

    def on_start(self, ctx: Context) -> None:
        self._last_quote: dict[str, int] = {}
        self._entry_order: dict[str, int] = {}
        self._exit_order: dict[str, int] = {}

    def on_bbo(self, ctx: Context, symbol: str, bbo: Bbo) -> None:
        if not bbo.valid:
            return
        pos = ctx.position(symbol)
        meta = ctx.meta(symbol)
        now = ctx.now
        if pos.qty == 0:
            oid = self._entry_order.get(symbol)
            imb = bbo.imbalance
            if oid is not None:
                o = next((x for x in ctx.orders(symbol) if x.id == oid), None)
                if o is None:
                    self._entry_order.pop(symbol, None)
                elif abs(imb) < float(self.p("threshold")) * 0.5 or (o.side is Side.BUY and o.price != bbo.bid) or (o.side is Side.SELL and o.price != bbo.ask):
                    ctx.cancel(oid)
                    self._entry_order.pop(symbol, None)
                return
            if now - self._last_quote.get(symbol, 0) < int(self.p("min_requote_ms")):
                return
            if abs(imb) >= float(self.p("threshold")):
                side = Side.BUY if imb > 0 else Side.SELL
                px = bbo.bid if side is Side.BUY else bbo.ask
                qty = ctx.qty_for_notional(symbol, float(self.p("notional_usd")), px)
                o = ctx.place_limit(symbol, side, px, qty, purpose=Purpose.ENTRY, tag="imb")
                if o is not None:
                    self._entry_order[symbol] = o.id
                    self._last_quote[symbol] = now
                    ctx.metric("imbalance_at_entry", imb, symbol)
        else:
            oid = self._exit_order.get(symbol)
            live = {x.id for x in ctx.orders(symbol)}
            if oid is not None and oid not in live:
                self._exit_order.pop(symbol, None)
                oid = None
            if now - pos.opened_ts > int(self.p("max_hold_ms")):
                if oid is not None:
                    ctx.cancel(oid)
                    self._exit_order.pop(symbol, None)
                ctx.close_position(symbol, taker=True, tag="timeout")
                return
            if oid is None:
                tp = int(self.p("take_profit_ticks")) * meta.tick_size
                if pos.qty > 0:
                    px = max(bbo.ask, meta.round_price(pos.avg_price + tp))
                    o = ctx.place_limit(symbol, Side.SELL, px, pos.qty, purpose=Purpose.EXIT, tag="tp", reduce_only=True)
                else:
                    px = min(bbo.bid, meta.round_price(pos.avg_price - tp))
                    o = ctx.place_limit(symbol, Side.BUY, px, abs(pos.qty), purpose=Purpose.EXIT, tag="tp", reduce_only=True)
                if o is not None:
                    self._exit_order[symbol] = o.id

    def on_fill(self, ctx: Context, fill: Fill) -> None:
        if fill.purpose is Purpose.ENTRY:
            self._entry_order.pop(fill.symbol, None)
