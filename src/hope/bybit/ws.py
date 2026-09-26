"""Публичный WebSocket Bybit V5: несколько соединений, батч-подписки, ping, реконнект, парсинг в события.

Топики: orderbook.{depth}.{symbol}, publicTrade.{symbol}, kline.{interval}.{symbol}, tickers.{symbol}.
События кладутся в asyncio.Queue; потребитель — движок.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time

import websockets

from ..types import BookEvent, Candle, KlineEvent, MarketEvent, Side, StatusEvent, TickerEvent, Trade, TradesEvent, now_ms

log = logging.getLogger(__name__)


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if ca and os.path.exists(ca):
        ctx.load_verify_locations(ca)
    return ctx


def parse_message(raw: str | bytes) -> MarketEvent | None:
    """Разобрать сообщение Bybit в событие. Служебные сообщения (pong, subscribe) -> None."""
    msg = json.loads(raw)
    topic = msg.get("topic")
    if not topic:
        return None
    data = msg.get("data")
    if topic.startswith("orderbook."):
        d = data
        return BookEvent(
            symbol=d["s"],
            ts=int(msg.get("ts") or 0),
            snapshot=msg.get("type") == "snapshot",
            bids=[(float(p), float(q)) for p, q in d.get("b", ())],
            asks=[(float(p), float(q)) for p, q in d.get("a", ())],
            seq=int(d.get("seq") or 0),
            update_id=int(d.get("u") or 0),
        )
    if topic.startswith("publicTrade."):
        trades = [
            Trade(ts=int(t["T"]), price=float(t["p"]), qty=float(t["v"]), taker_side=Side.BUY if t["S"] == "Buy" else Side.SELL)
            for t in data
        ]
        return TradesEvent(symbol=topic.split(".", 1)[1], trades=trades)
    if topic.startswith("kline."):
        _, interval, symbol = topic.split(".", 2)
        k = data[-1]
        c = Candle(
            ts_open=int(k["start"]),
            ts_close=int(k["end"]) + 1,
            open=float(k["open"]),
            high=float(k["high"]),
            low=float(k["low"]),
            close=float(k["close"]),
            volume=float(k.get("volume") or 0),
            turnover=float(k.get("turnover") or 0),
            closed=bool(k.get("confirm")),
        )
        return KlineEvent(symbol=symbol, interval=interval, candle=c)
    if topic.startswith("tickers."):
        d = data
        return TickerEvent(symbol=d.get("symbol") or topic.split(".", 1)[1], ts=int(msg.get("ts") or 0), fields=d)
    return None


class BybitPublicWS:
    """Менеджер публичных соединений. subscribe(topics) распределяет топики по соединениям."""

    PING_SECS = 20

    def __init__(self, url: str, out: asyncio.Queue, topics_per_connection: int = 200, args_per_subscribe: int = 50) -> None:
        self.url = url
        self.out = out
        self.topics_per_connection = max(1, topics_per_connection)
        self.args_per_subscribe = max(1, args_per_subscribe)
        self._conns: list[list[str]] = []
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.stats = {"messages": 0, "reconnects": 0, "last_msg_ts": 0}
        self._last_msg_mono: dict[int, float] = {}
        self._ssl = _ssl_context() if url.startswith("wss") else None

    def subscribe(self, topics: list[str]) -> None:
        topics = list(dict.fromkeys(topics))
        for i in range(0, len(topics), self.topics_per_connection):
            self._conns.append(topics[i : i + self.topics_per_connection])

    async def run(self) -> None:
        self._tasks = [asyncio.create_task(self._conn_loop(i, t), name=f"bybit-ws-{i}") for i, t in enumerate(self._conns)]
        try:
            await self._stop.wait()
        finally:
            for t in self._tasks:
                t.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(*self._tasks, return_exceptions=True), timeout=10)
            except asyncio.TimeoutError:
                log.warning("не все соединения закрылись за 10 с")

    def stop(self) -> None:
        self._stop.set()

    async def _conn_loop(self, idx: int, topics: list[str]) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.url, ssl=self._ssl, open_timeout=15, ping_interval=None, max_queue=4096, compression=None,
                ) as ws:
                    await self.out.put(StatusEvent(idx, f"connected, topics={len(topics)}", now_ms()))
                    for i in range(0, len(topics), self.args_per_subscribe):
                        await ws.send(json.dumps({"op": "subscribe", "req_id": f"{idx}-{i}", "args": topics[i : i + self.args_per_subscribe]}))
                    backoff = 1.0
                    self._last_msg_mono[idx] = time.monotonic()
                    pinger = asyncio.create_task(self._pinger(idx, ws))
                    try:
                        await self._reader(idx, ws)
                    except asyncio.CancelledError:
                        # остановка: рвём транспорт сразу, без close-handshake (он может зависнуть за прокси
                        # при большом входящем потоке и остановленном потребителе)
                        try:
                            ws.transport.abort()
                        except Exception:  # noqa: BLE001
                            pass
                        raise
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.stats["reconnects"] += 1
                await self.out.put(StatusEvent(idx, f"disconnected: {type(e).__name__}: {e}", now_ms()))
                log.warning("ws[%d] %s: %s; reconnect in %.0fs", idx, type(e).__name__, e, backoff)
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _pinger(self, idx: int, ws) -> None:
        """Ping раз в 20 с и сторожевой таймер: нет сообщений 60 с — рвём соединение (реконнект)."""
        while True:
            await asyncio.sleep(self.PING_SECS)
            if time.monotonic() - self._last_msg_mono.get(idx, 0.0) > self.PING_SECS * 3:
                log.warning("ws[%d]: нет сообщений %d с, переподключение", idx, self.PING_SECS * 3)
                ws.transport.abort()
                return
            await ws.send(json.dumps({"op": "ping"}))

    async def _reader(self, idx: int, ws) -> None:
        while True:
            raw = await ws.recv()
            self._last_msg_mono[idx] = time.monotonic()
            self.stats["messages"] += 1
            try:
                ev = parse_message(raw)
            except (KeyError, ValueError, TypeError) as e:
                log.debug("ws[%d] bad message: %s", idx, e)
                continue
            if ev is None:
                msg = json.loads(raw)
                if msg.get("op") == "subscribe" and not msg.get("success", True):
                    await self.out.put(StatusEvent(idx, f"subscribe failed: {msg.get('ret_msg')}", now_ms()))
                continue
            self.stats["last_msg_ts"] = int(time.time() * 1000)
            await self.out.put(ev)
