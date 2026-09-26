"""queue_mm — мейкерский сбор спреда с учётом очереди (порт идеи hftbacktest
«Queue-Based Market Making in Large Tick Size Assets») на движок hope.

Источник: https://github.com/nkaz001/hftbacktest/blob/master/examples/Queue-Based%20Market%20Making%20in%20Large%20Tick%20Size%20Assets.ipynb

Что взято из оригинала:
* резервационная цена = микроцена (book pressure) − skew · нормированная позиция;
* котировки на лучших ценах (half_spread ≈ 0.49 тика), т.е. «join»: встаём в очередь на BBO;
* чистая очередная модель: если объём на нашем уровне резко тает, ордер снимается
  (уровень вот-вот пробьют — исполнение будет прямо перед движением против нас);
* ограничение позиции и скос котировок по инвентарю.

Что добавлено для Bybit VIP0 (мейкер 0.02%, круг 4 б.п.) и paper-стенда:
* котируем только когда спред ≥ min_spread_bps (комиссии + edge); режим improve — на тик внутрь;
* активный набор символов по медиане спреда, потоку сделок и волатильности (как в spr);
* фильтр токсичного потока (дисбаланс тейкерских сделок) — не котируем сторону, которую «переедут»;
* тайм-аут позиции: сначала пассивный выход на тик внутрь, затем тейкерный;
* оценка ожидаемого времени до исполнения по очереди и обороту — не встаём в безнадёжные очереди.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from hope.engine.strategy import Context, Strategy
from hope.types import Bbo, Fill, Order, OrderDone, Purpose, Side


@dataclass(slots=True)
class SymState:
    bid_id: int | None = None
    ask_id: int | None = None
    last_requote: int = 0
    active: bool = False
    score: float = 0.0
    verdict: str = "warmup"
    level_qty_at_place: dict[int, float] = field(default_factory=dict)
    stale_taker_at: int = 0


class QueueMM(Strategy):
    name = "queue_mm"
    needs_trades = True
    needs_tickers = True
    orderbook_depth = 1

    @classmethod
    def default_params(cls):
        return {
            # --- котирование
            "min_spread_bps": 6.0,        # минимальный текущий спред, чтобы котировать (комиссии 4 б.п. + edge)
            "quote_mode": "join",         # join — встать в лучшую цену; improve — на тик внутрь, если спред позволяет
            "order_notional_usd": 100.0,
            "max_position_notional_usd": 300.0,
            "skew_ticks": 1.0,            # сдвиг резервационной цены, тиков на 100% лимита позиции
            "use_microprice": True,       # резервация от микроцены (book pressure), иначе от mid
            "min_requote_ms": 300,
            "max_order_age_ms": 20000,
            # --- очередь
            "max_expected_fill_secs": 90.0,   # не ставить ордер, если очередь впереди слишком длинна относительно оборота
            "depletion_cancel_ratio": 0.3,    # снять ордер, если объём уровня упал ниже доли от объёма при постановке
            # --- защита
            "toxicity_imbalance": 0.6,    # |дисбаланс тейкерского потока| за окно, выше — не котируем сторону под ударом
            "toxicity_window_secs": 10,
            "max_vol_bps": 25.0,          # минутная волатильность выше — только выходы
            "max_hold_secs": 120,         # позиция старше — пассивный выход на тик внутрь
            "stale_taker_secs": 60,       # ещё через столько секунд — тейкерный выход
            # --- активный набор символов
            "min_turnover_24h_usd": 3_000_000.0,
            "min_trades_per_min": 2.0,
            "min_spread_med_bps": 5.0,
            "min_spread_vol_ratio": 0.5,
            "max_active_symbols": 20,
            "refresh_secs": 15,
            "warmup_secs": 60,
        }

    # ------------------------------------------------------------------ жизненный цикл
    def on_start(self, ctx: Context) -> None:
        self.st: dict[str, SymState] = {s: SymState() for s in ctx.symbols}
        self._next_refresh = ctx.now + int(self.p("warmup_secs")) * 1000
        self._start_ts = ctx.now
        ctx.event(f"старт queue_mm: {len(ctx.symbols)} символов, min_spread={self.p('min_spread_bps')} б.п., режим={self.p('quote_mode')}")

    def on_stop(self, ctx: Context) -> None:
        ctx.cancel_all()

    # ------------------------------------------------------------------ активный набор
    def _score(self, ctx: Context, symbol: str) -> tuple[float, str]:
        st = ctx.stats(symbol)
        meta = ctx.meta(symbol)
        if meta.turnover_24h and meta.turnover_24h < float(self.p("min_turnover_24h_usd")):
            return 0.0, "оборот"
        if st.n_samples < 20:
            return 0.0, "warmup"
        spread = st.spread_med_bps
        vol = st.vol_bps
        tpm = st.trades_per_min
        if spread < max(float(self.p("min_spread_med_bps")), float(self.p("min_spread_bps"))):
            return 0.0, "спред"
        if tpm < float(self.p("min_trades_per_min")):
            return 0.0, "сделки/мин"
        if vol > 0 and spread / vol < float(self.p("min_spread_vol_ratio")):
            return 0.0, "спред/vol"
        return spread * math.sqrt(tpm) / (1.0 + vol), "ok"

    def _refresh_active(self, ctx: Context) -> None:
        scored = []
        for s in ctx.symbols:
            sc, verdict = self._score(ctx, s)
            self.st[s].score = sc
            self.st[s].verdict = verdict
            if sc > 0:
                scored.append((sc, s))
        scored.sort(reverse=True)
        top = {s for _, s in scored[: int(self.p("max_active_symbols"))]}
        for s in ctx.symbols:
            was = self.st[s].active
            self.st[s].active = s in top
            if was and not self.st[s].active:
                self._cancel_side(ctx, s, "bid_id")
                self._cancel_side(ctx, s, "ask_id")
            ctx.metric("score", self.st[s].score, s)
        ctx.metric("n_active", len(top))
        if top:
            ctx.event("активные: " + ", ".join(sorted(top)))

    # ------------------------------------------------------------------ таймер
    def on_timer(self, ctx: Context) -> None:
        if ctx.now >= self._next_refresh:
            self._next_refresh = ctx.now + int(self.p("refresh_secs")) * 1000
            self._refresh_active(ctx)
        max_age = int(self.p("max_order_age_ms"))
        for s in ctx.symbols:
            ss = self.st[s]
            for attr in ("bid_id", "ask_id"):
                oid = getattr(ss, attr)
                if oid is None:
                    continue
                o = self._order(ctx, oid)
                if o is None:
                    setattr(ss, attr, None)
                    continue
                if ctx.now - o.ts_created > max_age:
                    ctx.cancel(oid)
            bbo = ctx.bbo(s)
            if bbo is not None:
                self._quote(ctx, s, bbo)

    # ------------------------------------------------------------------ рынок
    def on_bbo(self, ctx: Context, symbol: str, bbo: Bbo) -> None:
        if not bbo.valid:
            return
        ss = self.st[symbol]
        # очередная модель: уровень тает — снимаем (только для ордеров-входов, стоящих на лучшей цене)
        ratio = float(self.p("depletion_cancel_ratio"))
        for attr, price, qty_now in (("bid_id", bbo.bid, bbo.bid_qty), ("ask_id", bbo.ask, bbo.ask_qty)):
            oid = getattr(ss, attr)
            if oid is None:
                continue
            o = self._order(ctx, oid)
            if o is None:
                setattr(ss, attr, None)
                continue
            q0 = ss.level_qty_at_place.get(oid)
            if q0 and o.price == price and o.purpose is Purpose.ENTRY and qty_now < ratio * q0 and o.queue_ahead > o.remaining:
                ctx.cancel(oid)
                ctx.metric("depletion_cancel", 1.0, symbol)
        self._quote(ctx, symbol, bbo)

    def on_fill(self, ctx: Context, fill: Fill) -> None:
        ss = self.st[fill.symbol]
        if ss.bid_id == fill.order_id and self._order(ctx, fill.order_id) is None:
            ss.bid_id = None
        if ss.ask_id == fill.order_id and self._order(ctx, fill.order_id) is None:
            ss.ask_id = None
        ss.stale_taker_at = 0
        pos = ctx.position(fill.symbol)
        ctx.metric("position_notional", pos.qty * fill.price, fill.symbol)

    def on_order_done(self, ctx: Context, done: OrderDone) -> None:
        ss = self.st[done.symbol]
        if ss.bid_id == done.order_id:
            ss.bid_id = None
        if ss.ask_id == done.order_id:
            ss.ask_id = None
        ss.level_qty_at_place.pop(done.order_id, None)

    # ------------------------------------------------------------------ логика котирования
    def _order(self, ctx: Context, oid: int) -> Order | None:
        for o in ctx.orders():
            if o.id == oid:
                return o
        return None

    def _cancel_side(self, ctx: Context, symbol: str, attr: str) -> None:
        ss = self.st[symbol]
        oid = getattr(ss, attr)
        if oid is not None:
            ctx.cancel(oid)

    def _desired(self, ctx: Context, symbol: str, bbo: Bbo) -> tuple[float | None, float | None, str]:
        """Желаемые цены bid/ask (None = не котировать сторону) и причина."""
        meta = ctx.meta(symbol)
        tick = meta.tick_size
        ss = self.st[symbol]
        pos = ctx.position(symbol)
        st = ctx.stats(symbol)
        max_pos = float(self.p("max_position_notional_usd"))
        pos_notional = pos.qty * bbo.mid
        pos_norm = max(-1.0, min(1.0, pos_notional / max_pos)) if max_pos > 0 else 0.0
        held = ctx.now - pos.opened_ts if pos.qty != 0 and pos.opened_ts else 0

        # 1. залежавшаяся позиция — только выход, агрессивнее
        if pos.qty != 0 and held > int(self.p("max_hold_secs")) * 1000:
            if held > (int(self.p("max_hold_secs")) + int(self.p("stale_taker_secs"))) * 1000:
                return None, None, "stale_taker"
            if pos.qty > 0:
                px = meta.round_price(bbo.ask - tick) if bbo.ask - tick > bbo.bid else bbo.ask
                return None, px, "stale_improve"
            px = meta.round_price(bbo.bid + tick) if bbo.bid + tick < bbo.ask else bbo.bid
            return px, None, "stale_improve"

        # 2. волатильность — только пассивный выход
        vol_ok = st.vol_bps <= float(self.p("max_vol_bps")) or st.n_samples < 10
        spread_bps = bbo.spread_bps
        min_spread = float(self.p("min_spread_bps"))
        spread_ok = spread_bps >= min_spread
        entries_ok = ss.active and vol_ok and spread_ok

        # 3. резервационная цена и котировки
        ref = bbo.microprice if self.p("use_microprice") else bbo.mid
        skew = float(self.p("skew_ticks")) * tick * pos_norm
        reservation = ref - skew
        half = 0.49 * tick
        bid = meta.round_price(math.floor((reservation - half) / tick) * tick)
        ask = meta.round_price(math.ceil((reservation + half) / tick) * tick)
        improve = self.p("quote_mode") == "improve"
        # не пересекаем рынок; join — не лучше лучшей цены; improve — можно на тик внутрь, если спред остаётся достаточным
        if improve and spread_bps - 2 * meta.tick_bps(bbo.mid) >= min_spread:
            bid = min(bid, meta.round_price(bbo.bid + tick))
            ask = max(ask, meta.round_price(bbo.ask - tick))
        else:
            bid = min(bid, bbo.bid)
            ask = max(ask, bbo.ask)
        if bid >= bbo.ask:
            bid = bbo.bid
        if ask <= bbo.bid:
            ask = bbo.ask

        # 4. токсичный поток: не котируем сторону, которую переедут
        imb = st.flow_imbalance(float(self.p("toxicity_window_secs")), ctx.now)
        tox = float(self.p("toxicity_imbalance"))
        bid_ok = entries_ok and imb > -tox
        ask_ok = entries_ok and imb < tox

        # 5. лимит позиции: при достижении — только сторона выхода; выходы разрешены всегда
        if pos.qty > 0:
            ask_ok = ask_ok or (spread_ok or not vol_ok)  # выход из лонга — всегда можно
            if pos_notional >= max_pos:
                bid_ok = False
        elif pos.qty < 0:
            bid_ok = bid_ok or (spread_ok or not vol_ok)
            if -pos_notional >= max_pos:
                ask_ok = False
        reason = "ok" if (bid_ok or ask_ok) else ("inactive" if not ss.active else "vol" if not vol_ok else "spread" if not spread_ok else "toxic")
        return (bid if bid_ok else None), (ask if ask_ok else None), reason

    def _qty_for(self, ctx: Context, symbol: str, side: Side, price: float) -> float:
        pos = ctx.position(symbol)
        notional = float(self.p("order_notional_usd"))
        qty = ctx.qty_for_notional(symbol, notional, price)
        # сторона выхода: не больше, чем нужно для закрытия плюс стандартный размер, но не перевернуться сверх лимита
        if (side is Side.SELL and pos.qty > 0) or (side is Side.BUY and pos.qty < 0):
            qty = max(qty, ctx.meta(symbol).round_qty_down(abs(pos.qty)))
            qty = min(qty, ctx.meta(symbol).round_qty_down(abs(pos.qty) + ctx.qty_for_notional(symbol, notional, price)))
        return qty

    def _expected_fill_ok(self, ctx: Context, symbol: str, price: float, level_qty: float) -> bool:
        """Ожидаемое время исполнения по очереди впереди и обороту в нашу сторону."""
        st = ctx.stats(symbol)
        tpm_usd = st.turnover_per_min
        if tpm_usd <= 0:
            return True
        queue_usd = level_qty * price
        secs = queue_usd / (tpm_usd / 2.0 / 60.0)
        return secs <= float(self.p("max_expected_fill_secs"))

    def _quote(self, ctx: Context, symbol: str, bbo: Bbo) -> None:
        ss = self.st[symbol]
        if ctx.now - ss.last_requote < int(self.p("min_requote_ms")):
            return
        bid, ask, reason = self._desired(ctx, symbol, bbo)
        pos = ctx.position(symbol)
        if reason == "stale_taker":
            if ss.stale_taker_at == 0:
                self._cancel_side(ctx, symbol, "bid_id")
                self._cancel_side(ctx, symbol, "ask_id")
                ctx.close_position(symbol, taker=True, tag="stale_taker")
                ss.stale_taker_at = ctx.now
                ss.last_requote = ctx.now
            return
        meta = ctx.meta(symbol)
        for attr, want, side, level_qty in (("bid_id", bid, Side.BUY, bbo.bid_qty), ("ask_id", ask, Side.SELL, bbo.ask_qty)):
            oid = getattr(ss, attr)
            cur = self._order(ctx, oid) if oid is not None else None
            if cur is None:
                setattr(ss, attr, None)
            if want is None:
                if cur is not None and cur.cancel_at is None:
                    ctx.cancel(cur.id)
                continue
            qty = self._qty_for(ctx, symbol, side, want)
            if qty <= 0:
                continue
            if cur is not None:
                if cur.price == want and abs(cur.remaining - qty) < meta.qty_step:
                    continue
                if cur.cancel_at is None:
                    ctx.cancel(cur.id)
                continue  # новый выставим на следующем цикле, когда отмена пройдёт
            reducing = (side is Side.SELL and pos.qty > 0) or (side is Side.BUY and pos.qty < 0)
            at_best = (want == bbo.bid) if side is Side.BUY else (want == bbo.ask)
            if not reducing and at_best and not self._expected_fill_ok(ctx, symbol, want, level_qty):
                ctx.metric("queue_skip", 1.0, symbol)
                continue
            purpose = Purpose.EXIT if reducing else Purpose.ENTRY
            o = ctx.place_limit(symbol, side, want, qty, post_only=True, purpose=purpose, tag=reason, reduce_only=reducing and qty <= abs(pos.qty) + 1e-12)
            if o is not None:
                setattr(ss, attr, o.id)
                ss.level_qty_at_place[o.id] = level_qty if at_best else 0.0
                ss.last_requote = ctx.now
