"""Устойчивость live-потока: свечи из kline, переключение на запасной адрес WebSocket."""

import asyncio
import json

from hope.bybit.ws import BybitPublicWS, parse_message
from hope.config import load_config
from hope.engine.core import EngineCore
from hope.engine.strategy import Strategy
from hope.market.candles import CandleBuilder
from hope.types import Candle, KlineEvent, Side, SymbolMeta, Trade, TradesEvent


def _kline(start, confirm, close=100.0):
    return {"start": start, "end": start + 59_999, "interval": "1", "open": "1", "high": "2", "low": "0.5",
            "close": str(close), "volume": "3", "turnover": "4", "confirm": confirm, "timestamp": start}


def test_parse_kline_returns_all_candles():
    raw = json.dumps({"topic": "kline.1.BTCUSDT", "type": "snapshot", "ts": 1,
                      "data": [_kline(60_000, True), _kline(120_000, False)]})
    evs = parse_message(raw)
    assert isinstance(evs, list) and len(evs) == 2
    assert evs[0].candle.closed and evs[0].candle.ts_open == 60_000 and evs[0].candle.ts_close == 120_000
    assert not evs[1].candle.closed and evs[1].symbol == "BTCUSDT" and evs[1].interval == "1"


class Collect(Strategy):
    name = "collect"
    candles_from_kline = True

    def on_start(self, ctx):
        self.closed = []

    def on_candle(self, ctx, symbol, candle):
        self.closed.append(candle.ts_open)


def test_live_candles_come_from_kline_not_trades(tmp_path):
    cfg = load_config(overrides={"run": {"data_dir": str(tmp_path)}, "exchange": {"symbols": ["XUSDT"]},
                                 "strategy": {"timeframe": "1m"}})
    strat = Collect()
    meta = SymbolMeta(symbol="XUSDT", tick_size=0.01, qty_step=0.001, source="rest")
    core = EngineCore(cfg, strat, None, "live", {"XUSDT": meta}, ["XUSDT"], use_book=False)
    assert core.kline_candles
    core.start(0)
    # сделки идут через две минуты — в kline-режиме они не создают и не закрывают свечи
    for ts in (1_000, 61_000, 125_000):
        core.handle(TradesEvent("XUSDT", [Trade(ts, 100.0, 1.0, Side.BUY)]), ts)
    core.tick(130_000)
    assert strat.closed == [] and len(core.candles("XUSDT")) == 0
    # подтверждённые свечи из kline; свеча 120 000 потерялась при переподключении — дорисуется плоской
    core.handle(KlineEvent("XUSDT", "1", Candle(0, 60_000, 1, 2, 0.5, 100.0, closed=True)), 60_000)
    core.handle(KlineEvent("XUSDT", "1", Candle(60_000, 120_000, 1, 2, 0.5, 101.0, closed=True)), 120_000)
    core.handle(KlineEvent("XUSDT", "1", Candle(180_000, 240_000, 1, 2, 0.5, 102.0, closed=True)), 240_000)
    series = core.candles("XUSDT")
    assert list(series.column("ts_open")) == [0, 60_000, 120_000, 180_000]
    assert series.column("close")[2] == 101.0  # плоская свеча по предыдущему close
    assert strat.closed == [0, 60_000, 180_000]


def test_backtest_still_builds_candles_from_trades(tmp_path):
    cfg = load_config(overrides={"run": {"data_dir": str(tmp_path)}, "exchange": {"symbols": ["XUSDT"]},
                                 "strategy": {"timeframe": "1m"}})
    core = EngineCore(cfg, Collect(), None, "backtest", {"XUSDT": SymbolMeta(symbol="XUSDT")}, ["XUSDT"], use_book=False)
    assert not core.kline_candles


def test_large_gap_is_not_filled():
    b = CandleBuilder("1m", 100)
    b.on_kline(Candle(0, 60_000, 1, 1, 1, 1, closed=True))
    b.on_kline(Candle(3_600_000, 3_660_000, 1, 1, 1, 2, closed=True))  # через час: разрыв прогрев -> live
    assert len(b.series) == 2


def test_ws_rotates_to_fallback_and_back():
    ws = BybitPublicWS("wss://a/v5/public/linear", asyncio.Queue(), fallback_urls=["wss://b/v5/public/linear"])
    assert ws.current_url(0) == "wss://a/v5/public/linear"
    # два коротких обрыва — ещё терпим, долгий сбрасывает счётчик
    assert ws.after_disconnect(0, 20) is None and ws.after_disconnect(0, 30) is None
    assert ws.after_disconnect(0, 600) is None
    assert ws.after_disconnect(0, 20) is None and ws.after_disconnect(0, 20) is None
    assert ws.after_disconnect(0, 20) == "wss://b/v5/public/linear"
    # запасной адрес не принимает подключения — возвращаемся на основной
    assert ws.after_disconnect(0, None) is None
    assert ws.after_disconnect(0, None) == "wss://a/v5/public/linear"
    # без запасных адресов не переключаемся никогда
    solo = BybitPublicWS("wss://a/x", asyncio.Queue())
    assert all(solo.after_disconnect(0, 1) is None for _ in range(10))


def test_proxy_modes():
    q = asyncio.Queue()
    assert BybitPublicWS("wss://a/x", q, proxy="none")._proxy_arg() is None
    assert BybitPublicWS("wss://a/x", q, proxy="auto")._proxy_arg() is True
    assert BybitPublicWS("wss://a/x", q, proxy="http://127.0.0.1:3128")._proxy_arg() == "http://127.0.0.1:3128"
    assert BybitPublicWS("wss://a/x", q, proxy="none").proxy_in_use() is None
