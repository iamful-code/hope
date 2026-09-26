from hope.paper.broker import PaperBroker, PlaceRequest
from hope.paper.portfolio import Portfolio
from hope.types import Bbo, Fill, Purpose, Side, Trade


def bbo(bid, ask, bq=1.0, aq=1.0, ts=0):
    return Bbo(ts=ts, bid=bid, ask=ask, bid_qty=bq, ask_qty=aq)


def req(side, price, qty, **kw):
    return PlaceRequest(symbol="X", side=side, price=price, qty=qty, **kw)


def test_queue_then_fill_on_trades():
    ex = PaperBroker(latency_ms=50, maker_fee=0.0002, taker_fee=0.00055)
    ex.on_bbo(0, "X", bbo(100.0, 100.1, 10.0, 10.0))
    o = ex.place(0, req(Side.BUY, 100.0, 5.0))
    ex.on_time(49)
    ex.on_trade(49, "X", Trade(49, 100.0, 100.0, Side.SELL))
    assert not ex.fills  # ещё pending
    ex.on_time(50)
    assert ex.order(o.id).queue_ahead == 10.0
    ex.on_trade(60, "X", Trade(60, 100.0, 8.0, Side.SELL))
    assert not ex.fills and ex.order(o.id).queue_ahead == 2.0
    ex.on_trade(61, "X", Trade(61, 100.0, 8.0, Side.BUY))  # тейкер-покупка не бьёт наш bid
    assert not ex.fills
    ex.on_trade(70, "X", Trade(70, 100.0, 5.0, Side.SELL))
    assert len(ex.fills) == 1 and ex.fills[0].qty == 3.0 and ex.order(o.id) is not None
    ex.on_trade(80, "X", Trade(80, 99.9, 0.1, Side.SELL))  # сквозь нашу цену
    assert len(ex.fills) == 2 and ex.fills[1].qty == 2.0 and ex.order(o.id) is None
    assert ex.done[0].status == "filled"
    assert abs(sum(f.fee for f in ex.fills) - 5.0 * 100.0 * 0.0002) < 1e-9
    assert all(f.is_maker for f in ex.fills)


def test_cancel_latency_and_partial():
    ex = PaperBroker(latency_ms=50)
    ex.on_bbo(0, "X", bbo(100.0, 100.1, 0.0, 10.0))
    o = ex.place(0, req(Side.BUY, 100.0, 5.0))
    ex.on_time(50)
    assert ex.cancel(60, o.id)
    ex.on_trade(70, "X", Trade(70, 100.0, 1.0, Side.SELL))
    assert len(ex.fills) == 1
    ex.on_time(110)
    assert ex.order(o.id) is None and ex.done[0].status == "partial_cancelled" and ex.done[0].filled == 1.0


def test_post_only_reject_and_sweep():
    ex = PaperBroker(latency_ms=0)
    ex.on_bbo(0, "X", bbo(100.0, 100.1))
    o = ex.place(0, req(Side.SELL, 100.0, 1.0))
    ex.on_time(0)
    assert ex.order(o.id) is None and ex.done[0].status == "rejected_post_only"
    o2 = ex.place(1, req(Side.SELL, 100.1, 1.0))
    ex.on_time(1)
    assert ex.order(o2.id).queue_ahead == 1.0
    ex.on_bbo(2, "X", bbo(100.2, 100.3))
    assert len(ex.fills) == 1 and ex.fills[0].price == 100.1 and ex.fills[0].side is Side.SELL


def test_crossing_limit_without_post_only_fills_as_taker():
    ex = PaperBroker(latency_ms=0, taker_slippage_bps=0.0)
    ex.on_bbo(0, "X", bbo(100.0, 100.1))
    o = ex.place(0, req(Side.BUY, 100.5, 1.0, post_only=False))
    ex.on_time(0)
    assert ex.order(o.id) is None and len(ex.fills) == 1
    assert ex.fills[0].price == 100.1 and not ex.fills[0].is_maker


def test_taker_order_with_slippage():
    ex = PaperBroker(latency_ms=10, taker_slippage_bps=10.0)
    ex.on_bbo(0, "X", bbo(100.0, 100.1, 1.0, 1.0))
    ex.place(0, req(Side.BUY, 0.0, 2.0, taker=True))
    ex.on_time(10)
    assert len(ex.fills) == 1
    assert abs(ex.fills[0].price - 100.1 * 1.001) < 1e-9
    assert not ex.fills[0].is_maker
    assert abs(ex.fills[0].fee - 2.0 * ex.fills[0].price * 0.00055) < 1e-9


def test_portfolio_avg_price_flip_and_pnl():
    pf = Portfolio(initial_equity=1000.0)

    def fill(side, price, qty, fee=0.0, ts=0):
        return Fill(order_id=1, symbol="X", side=side, price=price, qty=qty, fee=fee, ts=ts, is_maker=True, purpose=Purpose.ENTRY)

    pf.on_fill(fill(Side.BUY, 100.0, 1.0))
    pf.on_fill(fill(Side.BUY, 110.0, 1.0))
    p = pf.position("X")
    assert p.qty == 2.0 and abs(p.avg_price - 105.0) < 1e-9
    r = pf.on_fill(fill(Side.SELL, 120.0, 1.0, fee=0.1))
    assert abs(r - 15.0) < 1e-9 and p.qty == 1.0 and abs(p.avg_price - 105.0) < 1e-9
    # переворот: продаём 3 при позиции +1 -> закрываем 1 (pnl 5), открываем -2 по 110
    r = pf.on_fill(fill(Side.SELL, 110.0, 3.0))
    assert abs(r - 5.0) < 1e-9 and p.qty == -2.0 and p.avg_price == 110.0
    pf.on_mark("X", 100.0)
    assert abs(p.unrealized_pnl - 20.0) < 1e-9
    assert abs(pf.equity - (1000.0 + 20.0 - 0.1 + 20.0)) < 1e-9
    pf.on_fill(fill(Side.BUY, 100.0, 2.0))
    assert p.qty == 0.0 and abs(pf.realized_pnl - 40.0) < 1e-9
    # фандинг: шорт получает при положительной ставке
    pf.on_fill(fill(Side.SELL, 100.0, 1.0))
    amt = pf.on_funding("X", 0.0001, 100.0)
    assert abs(amt - 0.01) < 1e-12 and abs(pf.funding - 0.01) < 1e-12
