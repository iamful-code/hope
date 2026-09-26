"""Ядро движка: общее для live и бэктеста. Однопоточное по состоянию.

Поток событий: рыночное событие -> стакан/свечи/статистика -> paper-брокер (исполнения) ->
портфель -> хранилище -> обработчики стратегии. Таймер: активация ордеров, закрытие свечей,
снимки, риск, markout, фандинг.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..bybit.instruments import InstrumentCatalog, _decimals_of
from ..config import Config
from ..market.book import OrderBook
from ..market.candles import CandleBuilder, CandleSeries
from ..market.stats import RollingStats
from ..paper.broker import PaperBroker, PlaceRequest
from ..paper.portfolio import Portfolio, Position
from ..paper.risk import RiskManager
from ..store.db import Store
from ..types import (
    Bbo,
    BboEvent,
    BookEvent,
    Candle,
    Fill,
    KlineEvent,
    MarketEvent,
    Order,
    OrderDone,
    Purpose,
    Side,
    StatusEvent,
    SymbolMeta,
    TickerEvent,
    Trade,
    TradesEvent,
    bybit_kline_interval,
)
from .strategy import Context, Strategy

log = logging.getLogger("hope.engine")

MARKOUT_HORIZONS_MS = (1_000, 5_000, 30_000, 60_000)


@dataclass(slots=True)
class SymbolState:
    meta: SymbolMeta
    book: OrderBook | None
    candles: CandleBuilder
    stats: RollingStats
    bbo: Bbo | None = None
    ticker: dict = field(default_factory=dict)
    last_trade_ts: int = 0
    next_funding_ts: int = 0
    funding_rate: float = 0.0


@dataclass(slots=True)
class _PendingMarkout:
    seq: int
    symbol: str
    side: Side
    price: float
    ts: int
    horizons: list[int]


class EngineCore(Context):
    def __init__(self, cfg: Config, strategy: Strategy, store: Store | None, mode: str, metas: dict[str, SymbolMeta],
                 symbols: list[str], use_book: bool = True, taker_slippage_bps: float | None = None,
                 catalog: "InstrumentCatalog | None" = None) -> None:
        self.cfg = cfg
        self.catalog = catalog
        self.strategy = strategy
        self.store = store
        self.mode = mode
        self.params = strategy.params
        self.symbols = list(symbols)
        self.log = logging.getLogger(f"hope.strategy.{strategy.name}")
        self.now = 0
        self.broker = PaperBroker(
            latency_ms=cfg.paper.latency_ms,
            maker_fee=cfg.fees.maker,
            taker_fee=cfg.fees.taker,
            taker_slippage_bps=cfg.paper.taker_slippage_bps if taker_slippage_bps is None else taker_slippage_bps,
        )
        self.pf = Portfolio(initial_equity=cfg.paper.initial_equity_usd)
        self.risk = RiskManager(cfg.risk)
        self.sym: dict[str, SymbolState] = {}
        tf = cfg.strategy.timeframe
        for s in self.symbols:
            meta = metas.get(s) or SymbolMeta(symbol=s, category=cfg.exchange.category)
            self.sym[s] = SymbolState(
                meta=meta,
                book=OrderBook(meta) if use_book else None,
                candles=CandleBuilder(tf, cfg.strategy.history_bars),
                stats=RollingStats(120),
            )
        self.fill_seq = 0
        self._markouts: list[_PendingMarkout] = []
        self._next_timer = 0
        self._next_snapshot = 0
        self._last_risk_day = -1
        self.n_events = 0
        self.rejections = 0
        self.stopped = False

    # ================================================================ Context API
    def meta(self, symbol: str) -> SymbolMeta:
        return self.sym[symbol].meta

    def bbo(self, symbol: str) -> Bbo | None:
        return self.sym[symbol].bbo

    def book(self, symbol: str) -> OrderBook | None:
        return self.sym[symbol].book

    def candles(self, symbol: str) -> CandleSeries:
        return self.sym[symbol].candles.series

    def current_candle(self, symbol: str) -> Candle | None:
        return self.sym[symbol].candles.current

    def stats(self, symbol: str) -> RollingStats:
        return self.sym[symbol].stats

    def ticker(self, symbol: str) -> dict:
        return self.sym[symbol].ticker

    @property
    def portfolio(self) -> Portfolio:
        return self.pf

    def position(self, symbol: str) -> Position:
        return self.pf.position(symbol)

    def orders(self, symbol: str | None = None) -> list[Order]:
        return self.broker.live_orders(symbol)

    def qty_for_notional(self, symbol: str, notional_usd: float, price: float | None = None) -> float:
        st = self.sym[symbol]
        px = price or (st.bbo.mid if st.bbo and st.bbo.valid else st.candles.last_price)
        if not px or px <= 0:
            return 0.0
        return st.meta.round_qty_down(notional_usd / px)

    def _place(self, req: PlaceRequest) -> Order | None:
        st = self.sym.get(req.symbol)
        if st is None:
            self.log.warning("неизвестный символ %s", req.symbol)
            return None
        if req.qty <= 0:
            return None
        px = req.price if req.price > 0 else (st.bbo.mid if st.bbo and st.bbo.valid else st.candles.last_price)
        if px <= 0:
            self.log.debug("%s: нет цены, ордер отклонён", req.symbol)
            return None
        if px * req.qty < st.meta.min_notional - 1e-9:
            self.rejections += 1
            self.event(f"ордер {req.side.value} {req.qty} @ {px:.6g} меньше min_notional {st.meta.min_notional}", "warn", req.symbol)
            return None
        req.inventory_before = self.pf.qty(req.symbol)
        v = self.risk.allow_entry(self.pf, req.symbol, req.side, req.qty, px, self.broker.n_live(req.symbol))
        if not v.ok:
            self.rejections += 1
            self.event(f"риск отклонил {req.side.value} {req.qty} @ {px:.6g}: {v.reason}", "warn", req.symbol)
            return None
        return self.broker.place(self.now, req)

    def place_limit(self, symbol: str, side: Side, price: float, qty: float, *, post_only: bool = True,
                    purpose: Purpose = Purpose.ENTRY, tag: str = "", reduce_only: bool = False) -> Order | None:
        meta = self.sym[symbol].meta
        price = meta.round_price(price)
        qty = meta.round_qty_down(qty)
        return self._place(PlaceRequest(symbol, side, qty, price, taker=False, post_only=post_only, reduce_only=reduce_only,
                                        purpose=purpose, tag=tag))

    def place_market(self, symbol: str, side: Side, qty: float, *, purpose: Purpose = Purpose.ENTRY, tag: str = "",
                     reduce_only: bool = False) -> Order | None:
        qty = self.sym[symbol].meta.round_qty_down(qty)
        return self._place(PlaceRequest(symbol, side, qty, 0.0, taker=True, post_only=False, reduce_only=reduce_only,
                                        purpose=purpose, tag=tag))

    def cancel(self, order_id: int) -> bool:
        return self.broker.cancel(self.now, order_id)

    def cancel_all(self, symbol: str | None = None) -> int:
        return self.broker.cancel_all(self.now, symbol)

    def close_position(self, symbol: str, *, taker: bool = True, tag: str = "close") -> Order | None:
        q = self.pf.qty(symbol)
        if q == 0:
            return None
        side = Side.SELL if q > 0 else Side.BUY
        if taker:
            return self.place_market(symbol, side, abs(q), purpose=Purpose.EXIT, tag=tag, reduce_only=True)
        bbo = self.sym[symbol].bbo
        if bbo is None or not bbo.valid:
            return None
        px = bbo.ask if side is Side.SELL else bbo.bid
        return self.place_limit(symbol, side, px, abs(q), purpose=Purpose.EXIT, tag=tag, reduce_only=True)

    def metric(self, name: str, value: float, symbol: str = "") -> None:
        if self.store:
            self.store.metric(self.now, name, value, symbol)

    def event(self, msg: str, level: str = "info", symbol: str = "") -> None:
        getattr(self.log, "warning" if level == "warn" else level, self.log.info)("%s %s", symbol, msg)
        if self.store:
            self.store.event(self.now, level, msg, symbol)

    # ================================================================ события
    def start(self, now: int) -> None:
        self.now = now
        self._next_timer = now
        self._next_snapshot = now
        self.pf.update_drawdown(now)
        self.strategy.on_start(self)

    def stop(self, now: int) -> None:
        self.now = now
        if not self.stopped:
            self.stopped = True
            self.strategy.on_stop(self)
            self._after_broker()
            self._snapshot(now)

    def handle(self, ev: MarketEvent, now: int) -> None:
        self.now = now
        self.n_events += 1
        if isinstance(ev, BookEvent):
            st = self.sym.get(ev.symbol)
            if st is None or st.book is None:
                return
            if st.meta.source in ("default", "inferred"):
                self._infer_price_scale(st, ev)
            bbo = st.book.apply(ev)
            if bbo is not None:
                self._on_bbo(st, ev.symbol, bbo)
        elif isinstance(ev, BboEvent):
            st = self.sym.get(ev.symbol)
            if st is not None:
                self._on_bbo(st, ev.symbol, ev.bbo)
        elif isinstance(ev, TradesEvent):
            st = self.sym.get(ev.symbol)
            if st is None:
                return
            for t in ev.trades:
                self._on_trade(st, ev.symbol, t)
        elif isinstance(ev, KlineEvent):
            st = self.sym.get(ev.symbol)
            if st is None or ev.interval != bybit_kline_interval(self.cfg.strategy.timeframe):
                return
            if not self.cfg.exchange.subscribe_trades:  # свечи только из kline, если сделок нет
                closed = st.candles.on_kline(ev.candle)
                if closed is not None:
                    self.strategy.on_candle(self, ev.symbol, closed)
                    self._after_broker()
        elif isinstance(ev, TickerEvent):
            st = self.sym.get(ev.symbol)
            if st is None:
                return
            st.ticker.update(ev.fields)
            fr = ev.fields.get("fundingRate")
            nft = ev.fields.get("nextFundingTime")
            if fr not in (None, ""):
                st.funding_rate = float(fr)
            if nft not in (None, ""):
                st.next_funding_ts = int(nft)
            t24 = ev.fields.get("turnover24h")
            if t24 not in (None, ""):
                st.meta.turnover_24h = float(t24)
            self.strategy.on_ticker(self, ev.symbol, ev.fields)
        elif isinstance(ev, StatusEvent):
            self.event(f"ws[{ev.conn}] {ev.msg}", "info")
        self._after_broker()

    def _infer_price_scale(self, st: SymbolState, ev: BookEvent) -> None:
        """Каталог не знает символ: уточняем шаг цены по потоку; при смене шага стакан пересобирается."""
        if self.catalog is None:
            d = max((_decimals_of(p) for p, _ in (ev.bids[:3] + ev.asks[:3])), default=0)
            if d > st.meta.price_scale or st.meta.source == "default":
                st.meta.price_scale = max(d, st.meta.price_scale if st.meta.source != "default" else 0)
                st.meta.tick_size = 10.0 ** (-st.meta.price_scale)
                st.meta.source = "inferred"
                if st.book is not None:
                    st.book.clear()
            return
        prices = [p for p, _ in ev.bids[:5]] + [p for p, _ in ev.asks[:5]]
        if self.catalog.observe_price(ev.symbol, *prices) and st.book is not None:
            st.book.clear()
            if not ev.snapshot:
                st.book.dirty = True  # дельта без снимка: BBO пересчитается на следующем snapshot

    def _on_bbo(self, st: SymbolState, symbol: str, bbo: Bbo) -> None:
        st.bbo = bbo
        self.broker.on_bbo(self.now, symbol, bbo)
        st.stats.on_bbo(bbo)
        if bbo.valid:
            self.pf.on_mark(symbol, bbo.mid)
        if self.store and self.cfg.store.record_market:
            self.store.md_bbo(bbo.ts, symbol, bbo.bid, bbo.ask, bbo.bid_qty, bbo.ask_qty)
        self.strategy.on_bbo(self, symbol, bbo)

    def _on_trade(self, st: SymbolState, symbol: str, t: Trade) -> None:
        if st.meta.source in ("default", "inferred"):
            d = _decimals_of(t.qty)
            if 10.0 ** (-d) < st.meta.qty_step:
                st.meta.qty_step = 10.0 ** (-d)
                st.meta.min_qty = st.meta.qty_step
            if self.catalog is not None:
                self.catalog.observe_price(symbol, t.price)
        st.last_trade_ts = t.ts
        self.broker.on_trade(self.now, symbol, t)
        st.stats.on_trade(t)
        if st.bbo is None:
            self.pf.on_mark(symbol, t.price)
        if self.store and self.cfg.store.record_market:
            self.store.md_trade(t.ts, symbol, t.taker_side.value, t.price, t.qty)
        closed = st.candles.on_trade(t)
        self.strategy.on_trade(self, symbol, t)
        if closed is not None:
            self.strategy.on_candle(self, symbol, closed)

    # ================================================================ таймер
    def tick(self, now: int) -> None:
        """Вызывать регулярно (live: ~каждые 100 мс; backtest: перед каждым событием, если время пришло)."""
        self.now = now
        self.broker.on_time(now)
        # закрытие свечей без сделок
        for s, st in self.sym.items():
            closed = st.candles.on_time(now)
            if closed is not None:
                self.strategy.on_candle(self, s, closed)
        self._after_broker()
        self._process_markouts(now)
        self._process_funding(now)
        if now >= self._next_timer:
            self._next_timer = now + self.cfg.strategy.timer_ms
            self.strategy.on_timer(self)
            self._after_broker()
        if now >= self._next_snapshot:
            self._next_snapshot = now + int(self.cfg.store.snapshot_secs * 1000)
            self._snapshot(now)

    def _after_broker(self) -> None:
        """Забрать исполнения из брокера, провести по портфелю, записать, уведомить стратегию."""
        fills, done = self.broker.drain()
        while fills or done:
            for f in fills:
                self.pf.on_fill(f)
                self.fill_seq += 1
                if self.store:
                    self.store.fill(f, self.fill_seq)
                self._markouts.append(_PendingMarkout(self.fill_seq, f.symbol, f.side, f.price, f.ts, list(MARKOUT_HORIZONS_MS)))
                self.strategy.on_fill(self, f)
            for d in done:
                if self.store:
                    self.store.order_done(d)
                self.strategy.on_order_done(self, d)
            fills, done = self.broker.drain()
        self.pf.update_drawdown(self.now)
        self.risk.check_portfolio(self.pf)

    def _process_markouts(self, now: int) -> None:
        if not self._markouts:
            return
        keep: list[_PendingMarkout] = []
        for m in self._markouts:
            st = self.sym[m.symbol]
            mid = st.bbo.mid if st.bbo and st.bbo.valid else st.candles.last_price
            while m.horizons and now - m.ts >= m.horizons[0]:
                h = m.horizons.pop(0)
                if mid > 0 and self.store:
                    bps = m.side.sign * (mid - m.price) / m.price * 1e4
                    self.store.markout(m.seq, m.symbol, h, mid, bps)
            if m.horizons:
                keep.append(m)
        self._markouts = keep

    def _process_funding(self, now: int) -> None:
        if not self.cfg.paper.apply_funding or self.cfg.exchange.category == "spot":
            return
        for s, st in self.sym.items():
            if st.next_funding_ts and now >= st.next_funding_ts and self.pf.qty(s) != 0:
                mark = st.bbo.mid if st.bbo and st.bbo.valid else st.candles.last_price
                amt = self.pf.on_funding(s, st.funding_rate, mark)
                self.event(f"фандинг {st.funding_rate:+.6f}: {amt:+.4f} USDT", "info", s)
                st.next_funding_ts += 8 * 3_600_000

    def _snapshot(self, now: int) -> None:
        if not self.store:
            return
        snap = self.pf.snapshot()
        self.store.equity(now, snap, self.broker.n_live())
        for p in self.pf.positions.values():
            if p.qty != 0 or p.n_fills:
                self.store.position(now, p)
        for s, st in self.sym.items():
            if st.bbo is None:
                continue
            self.store.symbol_stats(
                now, s, st.bbo.mid, st.bbo.spread_bps, st.stats.spread_med_bps, st.stats.vol_bps,
                st.stats.trades_per_min, st.stats.turnover_per_min, st.stats.flow_imbalance(10, now),
            )

    # ================================================================ итоги
    def summary(self) -> dict[str, Any]:
        snap = self.pf.snapshot()
        snap.update(
            {
                "initial_equity": self.pf.initial_equity,
                "net_pnl": self.pf.equity - self.pf.initial_equity,
                "return_pct": (self.pf.equity / self.pf.initial_equity - 1) * 100,
                "n_events": self.n_events,
                "rejections": self.rejections,
                "kill_switch": self.risk.killed,
                "instruments": {
                    s: {"tick_size": st.meta.tick_size, "qty_step": st.meta.qty_step, "source": st.meta.source}
                    for s, st in self.sym.items()
                },
            }
        )
        return snap
