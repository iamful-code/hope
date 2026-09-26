"""Экспорт результатов работающего Freqtrade в схему hope через его REST API.

Зачем: стратегии Freqtrade запускаются «как есть» настоящим ботом (dry-run на Bybit), а в мониторе
hope они должны выглядеть как обычный запуск: `runs.engine='freqtrade'`, исполнения (`fills`),
ордера (`orders`), снимки equity и позиции. Экспортёр раз в `interval_secs` опрашивает API
(`/api/v1/show_config`, `/status`, `/trades`, `/profit`, `/balance`, `/whitelist`) и дописывает
в БД только новое; состояние (run_id, экспортированные ордера, счётчик seq, позиции)
хранится в небольшом JSON рядом с БД, поэтому рестарт продолжает тот же запуск.

Соответствие полей (подробнее — adapters/freqtrade/README.md):
  сделка Freqtrade (trade)       -> ничего напрямую; её ордера -> fills/orders, закрытие -> events
  order.ft_order_side buy/sell   -> fills.side Buy/Sell; 'stoploss' -> сторона выхода, purpose 'stop'
  order.average|price            -> fills.price; order.filled -> fills.qty
  trade.fee_open / fee_close     -> fills.fee = qty*price*ставка (вход / выход)
  order.order_type == 'limit'    -> fills.is_maker
  trade.profit_abs (+комиссии,   -> fills.realized_pnl ордеров выхода, пропорционально их объёму
    -фандинг = «грязный» PnL)
  /profit.profit_all_coin        -> equity = initial_equity + profit_all_coin
  открытые сделки (/status)      -> positions (qty со знаком, avg=open_rate, mark=current_rate)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from ...store.db import Store
from ...types import Fill, OrderDone, Purpose, Side, now_ms

log = logging.getLogger("hope.adapters.freqtrade.exporter")

ENGINE = "freqtrade"
STATE_VERSION = 1
PAGE_SIZE = 100  # размер страницы /api/v1/trades
MAX_BACKOFF_SECS = 300.0
_EPS = 1e-12


class FreqtradeApiError(RuntimeError):
    """Ошибка обращения к REST API Freqtrade: сеть, авторизация, не-JSON ответ."""


# ====================================================================== утилиты
def pair_to_symbol(pair: str) -> str:
    """Пара Freqtrade/ccxt -> символ Bybit: 'BTC/USDT:USDT' -> 'BTCUSDT', '1000PEPE/USDT' -> '1000PEPEUSDT'."""
    base, _, rest = pair.partition("/")
    quote = rest.split(":")[0]
    return f"{base}{quote}".upper()


def _num(x: Any, default: float = 0.0) -> float:
    """float без NaN/inf/None (Freqtrade отдаёт NaN, если цена недоступна)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


def _int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


# ====================================================================== REST-клиент
class FreqtradeClient:
    """Минимальный синхронный клиент REST API Freqtrade (HTTP basic auth)."""

    def __init__(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        auth = (username, password or "") if username else None
        self._c = httpx.Client(base_url=self.url, auth=auth, timeout=timeout, transport=transport)

    def get(self, path: str, params: dict | None = None) -> Any:
        try:
            r = self._c.get(f"/api/v1/{path}", params=params)
        except httpx.HTTPError as e:
            raise FreqtradeApiError(f"{path}: {e.__class__.__name__}: {e}") from e
        if r.status_code != 200:
            raise FreqtradeApiError(f"{path}: HTTP {r.status_code} {r.text[:200]!r}")
        try:
            return r.json()
        except ValueError as e:
            raise FreqtradeApiError(f"{path}: ответ не JSON: {r.text[:200]!r}") from e

    def ping(self) -> dict:
        return self.get("ping")

    def show_config(self) -> dict:
        return self.get("show_config")

    def status(self) -> list[dict]:
        """Открытые сделки (пустой список, если их нет)."""
        return self.get("status") or []

    def trades(self, limit: int = PAGE_SIZE, offset: int = 0, order_by_id: bool = False) -> dict:
        """Закрытые сделки; order_by_id=False — по времени закрытия, новые первыми."""
        return self.get("trades", {"limit": limit, "offset": offset, "order_by_id": order_by_id})

    def profit(self) -> dict:
        return self.get("profit")

    def balance(self) -> dict:
        return self.get("balance")

    def whitelist(self) -> list[str]:
        d = self.get("whitelist")
        if isinstance(d, dict):
            return list(d.get("whitelist") or [])
        return list(d or [])

    def close(self) -> None:
        self._c.close()


# ====================================================================== состояние
@dataclass(slots=True)
class _Pos:
    """Позиция по символу, которую ведёт экспортёр по экспортированным исполнениям.

    Совместима с Store.position(): symbol, qty, avg_price, mark, unrealized_pnl, realized_pnl,
    fees, funding, n_fills, opened_ts.
    """

    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    n_fills: int = 0
    opened_ts: int = 0
    mark: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass(slots=True)
class _Pending:
    """Исполнение, подготовленное к записи (сортируем по времени перед применением к позициям)."""

    ts: int
    trade_id: int
    idx: int
    fill: Fill
    order_key: str
    delta: float
    trade_open: bool
    realized: float | None  # None -> считаем по средней цене позиции (частичный выход открытой сделки)


def _default_state(url: str) -> dict:
    return {
        "version": STATE_VERSION,
        "url": url,
        "run_id": None,
        "bot_name": None,
        "strategy": None,
        "initial_equity": 0.0,
        "seq": 0,
        "n_fills": 0,
        "next_order_n": 1,
        "orders": {},  # ft order_id -> {"n": int, "qty": экспортированный объём, "done": bool}
        "closed_trades": [],  # id сделок, по которым записано событие закрытия
        "trade_assigned": {},  # trade_id -> уже присвоенный ордерам выхода «грязный» PnL (открытые сделки)
        "positions": {},  # symbol -> поля _Pos
        "open_symbols": [],  # символы с открытой сделкой на прошлом опросе
        "peak_equity": 0.0,
        "max_drawdown": 0.0,
        "realized_total": 0.0,
        "fees_total": 0.0,
        "funding_closed": 0.0,
        "last_equity_ts": 0,
    }


# ====================================================================== экспортёр
class FreqtradeExporter:
    """Опрашивает REST API Freqtrade и пишет запуск `engine='freqtrade'` в БД hope.

    `poll_once()` — один синхронный цикл (бросает FreqtradeApiError при проблемах с API);
    `run()` — асинхронный цикл с бэкоффом, который никогда не падает на временных ошибках.
    """

    def __init__(
        self,
        url: str,
        username: str | None,
        password: str | None,
        store_path: str | Path,
        run_name: str | None = None,
        interval_secs: float = 10.0,
        state_path: str | Path | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.run_name = run_name
        self.interval = float(interval_secs)
        self.store_path = Path(store_path)
        self.state_path = Path(state_path) if state_path else Path(str(self.store_path) + ".ftexport.json")
        self.client = FreqtradeClient(self.url, username, password, timeout=timeout, transport=transport)
        self.store = Store(self.store_path, batch_ms=200)
        self.state = self._load_state()
        self._positions: dict[str, _Pos] = {
            s: _Pos(**{k: v for k, v in d.items() if k in _Pos.__slots__})  # type: ignore[attr-defined]
            for s, d in self.state.get("positions", {}).items()
        }
        self._closed: set[int] = {int(x) for x in self.state.get("closed_trades", [])}
        self._connected = False
        self._closed_flag = False
        self._resumed = False  # запуск из состояния уже «возобновлён» в этом процессе (status=running)

    # ---------------------------------------------------------------- состояние
    def _load_state(self) -> dict:
        st = _default_state(self.url)
        try:
            if self.state_path.exists():
                loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("version") == STATE_VERSION:
                    st.update(loaded)
                    log.info("состояние экспортёра загружено: %s (run_id=%s)", self.state_path, st.get("run_id"))
                else:
                    log.warning("состояние %s несовместимо, начинаю заново", self.state_path)
        except (OSError, ValueError) as e:
            log.warning("не удалось прочитать состояние %s: %s", self.state_path, e)
        st["url"] = self.url
        return st

    def _save_state(self) -> None:
        self.state["positions"] = {s: asdict(p) for s, p in self._positions.items()}
        self.state["closed_trades"] = sorted(self._closed)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.error("не удалось сохранить состояние %s: %s", self.state_path, e)

    def _reset_state(self) -> None:
        keep_url = self.state.get("url", self.url)
        self.state = _default_state(keep_url)
        self._positions = {}
        self._closed = set()

    # ---------------------------------------------------------------- запуск в БД
    def _run_exists(self, run_id: int) -> bool:
        try:
            row = self.store.con.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone()
        except Exception:  # noqa: BLE001 — битая БД: создадим новый запуск
            return False
        return row is not None

    def _ensure_run(self, cfg: dict) -> None:
        """Создать запуск при первом успешном show_config либо продолжить прежний из состояния."""
        bot_name = str(cfg.get("bot_name") or "freqtrade")
        strategy = str(cfg.get("strategy") or "unknown")
        st = self.state
        if st.get("run_id") and st.get("bot_name") == bot_name and st.get("strategy") == strategy:
            if self._run_exists(int(st["run_id"])):
                if not self._resumed:
                    self._resumed = True
                    self.store.run_id = int(st["run_id"])
                    self.store.con.execute(
                        "UPDATE runs SET status='running', finished_ts=NULL WHERE run_id=?", (self.store.run_id,)
                    )
                    log.info("продолжаю запуск run_id=%s (%s / %s)", self.store.run_id, bot_name, strategy)
                return
            log.warning("run_id=%s из состояния не найден в БД — создаю новый запуск", st["run_id"])
        elif st.get("run_id"):
            log.info(
                "бот/стратегия изменились (%s/%s -> %s/%s): новый запуск",
                st.get("bot_name"), st.get("strategy"), bot_name, strategy,
            )
        self._reset_state()

        initial = 0.0
        try:
            bal = self.client.balance()
            initial = _num(bal.get("starting_capital")) or _num(bal.get("total"))
        except FreqtradeApiError as e:
            log.warning("balance недоступен (%s): initial_equity возьму из конфига", e)
        if initial <= 0:
            initial = _num(cfg.get("available_capital")) or _num(cfg.get("dry_run_wallet"))
        symbols: list[str] = []
        try:
            symbols = [pair_to_symbol(p) for p in self.client.whitelist()]
        except FreqtradeApiError as e:
            log.warning("whitelist недоступен: %s", e)
        started_ts = None
        try:
            started_ts = _int(self.client.profit().get("bot_start_timestamp")) or None
        except FreqtradeApiError:
            pass

        name = self.run_name or f"ft:{strategy}:{bot_name}"
        mode = "live" if cfg.get("dry_run", True) else "live-real"
        params = {
            "bot_name": bot_name,
            "url": self.url,
            "exchange": cfg.get("exchange"),
            "trading_mode": cfg.get("trading_mode"),
            "timeframe": cfg.get("timeframe"),
            "stake_amount": cfg.get("stake_amount"),
            "max_open_trades": cfg.get("max_open_trades"),
            "dry_run": cfg.get("dry_run"),
            "freqtrade_version": cfg.get("version"),
        }
        run_id = self.store.start_run(
            name, strategy, mode, initial, symbols, config=cfg, params=params, engine=ENGINE, started_ts=started_ts
        )
        st = self.state
        st.update({"run_id": run_id, "bot_name": bot_name, "strategy": strategy, "initial_equity": initial,
                   "peak_equity": initial})
        self._resumed = True
        log.info("создан запуск run_id=%s: %s, стратегия %s, initial_equity=%.2f, символов=%d",
                 run_id, name, strategy, initial, len(symbols))
        self._save_state()

    # ---------------------------------------------------------------- загрузка данных
    def _fetch_closed(self) -> list[dict]:
        """Закрытые сделки постранично (новые первыми); останавливаемся на странице из одних известных."""
        out: list[dict] = []
        offset = 0
        seen: set[int] = set()
        while True:
            page = self.client.trades(limit=PAGE_SIZE, offset=offset, order_by_id=False)
            trades = page.get("trades") or []
            fresh = 0
            for t in trades:
                tid = _int(t.get("trade_id"))
                if tid in seen:
                    continue
                seen.add(tid)
                out.append(t)
                if tid not in self._closed:
                    fresh += 1
            if len(trades) < PAGE_SIZE or fresh == 0:
                break
            offset += PAGE_SIZE
        return out

    # ---------------------------------------------------------------- исполнения
    def _pos(self, symbol: str) -> _Pos:
        p = self._positions.get(symbol)
        if p is None:
            p = _Pos(symbol)
            self._positions[symbol] = p
        return p

    def _order_rec(self, key: str) -> dict:
        rec = self.state["orders"].get(key)
        if rec is None:
            rec = {"n": int(self.state["next_order_n"]), "qty": 0.0, "done": False}
            self.state["next_order_n"] = rec["n"] + 1
            self.state["orders"][key] = rec
        return rec

    @staticmethod
    def _model_realized(p: _Pos, side: Side, price: float, qty: float) -> float:
        """«Грязный» PnL закрытия части позиции по средней цене (как в paper-портфеле hope)."""
        signed = qty if side is Side.BUY else -qty
        if p.qty == 0 or (p.qty > 0) == (signed > 0):
            return 0.0
        closing = min(abs(signed), abs(p.qty))
        direction = 1.0 if p.qty > 0 else -1.0
        return (price - p.avg_price) * closing * direction

    @staticmethod
    def _apply_to_position(p: _Pos, f: Fill) -> None:
        signed = f.qty if f.side is Side.BUY else -f.qty
        if p.qty == 0 or (p.qty > 0) == (signed > 0):
            new_qty = p.qty + signed
            p.avg_price = (p.avg_price * abs(p.qty) + f.price * abs(signed)) / abs(new_qty)
            if p.qty == 0:
                p.opened_ts = f.ts
            p.qty = new_qty
        else:
            closing = min(abs(signed), abs(p.qty))
            remaining = abs(signed) - closing
            new_qty = p.qty + signed
            if abs(new_qty) < _EPS:
                p.qty, p.avg_price, p.opened_ts = 0.0, 0.0, 0
            elif remaining > _EPS:
                p.qty, p.avg_price, p.opened_ts = new_qty, f.price, f.ts
            else:
                p.qty = new_qty
        p.realized_pnl += f.realized_pnl
        p.fees += f.fee
        p.n_fills += 1
        p.mark = f.price if p.mark <= 0 else p.mark
        f.position_after = p.qty

    def _collect_trade(self, t: dict, now: int) -> tuple[list[_Pending], list[tuple[str, OrderDone]]]:
        """Новые исполнения и завершённые ордера одной сделки (не пишет в БД, не меняет состояние)."""
        tid = _int(t.get("trade_id"))
        symbol = pair_to_symbol(str(t.get("pair") or ""))
        is_short = bool(t.get("is_short"))
        is_open = bool(t.get("is_open"))
        entry_side = Side.SELL if is_short else Side.BUY
        exit_side = entry_side.opposite
        fee_open = _num(t.get("fee_open"))
        fee_close = _num(t.get("fee_close"))
        close_ts = _int(t.get("close_timestamp")) or now
        orders = [o for o in (t.get("orders") or []) if isinstance(o, dict)]

        # заполненные и уже не открытые ордера; is_open ордера ждём (частичное исполнение допишем позже)
        done = [o for o in orders if not o.get("is_open") and _num(o.get("filled")) > _EPS]
        fees_trade = 0.0
        for o in done:
            rate = fee_open if o.get("ft_is_entry") else fee_close
            price = _num(o.get("average")) or _num(o.get("price")) or _num(o.get("safe_price"))
            fees_trade += _num(o.get("filled")) * price * rate
        gross_total = _num(t.get("profit_abs")) + fees_trade - _num(t.get("funding_fees"))
        remaining_gross = gross_total - _num(self.state["trade_assigned"].get(str(tid)))

        pend: list[_Pending] = []
        dones: list[tuple[str, OrderDone]] = []
        new_exit_qty = 0.0
        news: list[tuple[int, dict, str, float]] = []
        for idx, o in enumerate(done):
            key = f"{tid}:{o.get('order_id')}"
            rec = self.state["orders"].get(key)
            exported = _num(rec["qty"]) if rec else 0.0
            delta = _num(o.get("filled")) - exported
            if delta > _EPS:
                news.append((idx, o, key, delta))
                if not o.get("ft_is_entry"):
                    new_exit_qty += delta
            if not (rec and rec.get("done")):
                dones.append((key, self._order_done(o, t, key, entry_side, exit_side, now)))

        for idx, o, key, delta in news:
            side_str = str(o.get("ft_order_side") or "")
            is_stop = side_str == "stoploss"
            is_entry = bool(o.get("ft_is_entry"))
            side = entry_side if is_entry else exit_side
            if not is_stop and side_str in ("buy", "sell"):
                side = Side.parse(side_str)
            price = _num(o.get("average")) or _num(o.get("price")) or _num(o.get("safe_price"))
            rate = fee_open if is_entry else fee_close
            ts = _int(o.get("order_filled_timestamp")) or close_ts
            purpose = Purpose.STOP if is_stop else (Purpose.ENTRY if is_entry else Purpose.EXIT)
            tag = str(o.get("ft_order_tag") or (t.get("enter_tag") if is_entry else t.get("exit_reason")) or "")
            realized: float | None = 0.0
            if not is_entry:
                if is_open:
                    realized = None  # по средней цене позиции, при закрытии сделки скорректируем остаток
                else:
                    realized = remaining_gross * delta / new_exit_qty if new_exit_qty > _EPS else 0.0
            f = Fill(
                order_id=0,  # проставим при записи (целочисленный id из состояния)
                symbol=symbol,
                side=side,
                price=price,
                qty=delta,
                fee=delta * price * rate,
                ts=ts,
                is_maker=str(o.get("order_type") or "").lower() == "limit",
                purpose=purpose,
                tag=tag,
                placed_ts=_int(o.get("order_timestamp")),
            )
            pend.append(_Pending(ts, tid, idx, f, key, delta, is_open, realized))
        return pend, dones

    @staticmethod
    def _order_done(o: dict, t: dict, key: str, entry_side: Side, exit_side: Side, now: int) -> OrderDone:
        is_entry = bool(o.get("ft_is_entry"))
        side_str = str(o.get("ft_order_side") or "")
        is_stop = side_str == "stoploss"
        side = entry_side if is_entry else exit_side
        if not is_stop and side_str in ("buy", "sell"):
            side = Side.parse(side_str)
        price = _num(o.get("price")) or _num(o.get("average")) or _num(o.get("safe_price"))
        ts_done = _int(o.get("order_filled_timestamp")) or _int(t.get("close_timestamp")) or now
        status = "filled" if str(o.get("status") or "") == "closed" else "cancelled"
        purpose = Purpose.STOP if is_stop else (Purpose.ENTRY if is_entry else Purpose.EXIT)
        tag = str(o.get("ft_order_tag") or (t.get("enter_tag") if is_entry else t.get("exit_reason")) or "")
        return OrderDone(
            order_id=0,
            symbol=pair_to_symbol(str(t.get("pair") or "")),
            side=side,
            price=price,
            qty=_num(o.get("amount")),
            filled=_num(o.get("filled")),
            ts_created=_int(o.get("order_timestamp")) or ts_done,
            ts_done=ts_done,
            status=status,
            purpose=purpose,
            tag=tag,
            taker=str(o.get("order_type") or "").lower() != "limit",
            queue_ahead_initial=0.0,
            spread_bps_at_place=0.0,
        )

    def _export_fills(self, closed: list[dict], open_trades: list[dict], now: int) -> int:
        pend: list[_Pending] = []
        dones: list[tuple[str, OrderDone]] = []
        for t in closed:
            if _int(t.get("trade_id")) in self._closed:
                continue
            p, d = self._collect_trade(t, now)
            pend += p
            dones += d
        for t in open_trades:
            p, d = self._collect_trade(t, now)
            pend += p
            dones += d

        pend.sort(key=lambda x: (x.ts, x.trade_id, x.idx))
        st = self.state
        for x in pend:
            rec = self._order_rec(x.order_key)
            f = x.fill
            f.order_id = rec["n"]
            pos = self._pos(f.symbol)
            f.inventory_before = pos.qty
            if x.realized is None:
                r = self._model_realized(pos, f.side, f.price, f.qty)
                key = str(x.trade_id)
                st["trade_assigned"][key] = _num(st["trade_assigned"].get(key)) + r
                f.realized_pnl = r
            else:
                f.realized_pnl = x.realized
            self._apply_to_position(pos, f)
            st["seq"] = int(st["seq"]) + 1
            self.store.fill(f, st["seq"])
            rec["qty"] = _num(rec["qty"]) + x.delta
            st["n_fills"] = int(st["n_fills"]) + 1
            st["realized_total"] = _num(st["realized_total"]) + f.realized_pnl
            st["fees_total"] = _num(st["fees_total"]) + f.fee
            log.info("исполнение #%d %s %s %.8g @ %.8g (%s, %s) pnl=%.4f pos=%.8g", st["seq"], f.symbol, f.side.value,
                     f.qty, f.price, f.purpose.value, f.tag, f.realized_pnl, f.position_after)
        for key, d in dones:
            rec = self._order_rec(key)
            d.order_id = rec["n"]
            self.store.order_done(d)
            rec["done"] = True
        return len(pend)

    def _finalize_closed(self, closed: list[dict]) -> int:
        """Событие по каждой новой закрытой сделке; фандинг закрытых — в накопитель."""
        n = 0
        st = self.state
        for t in sorted(closed, key=lambda t: _int(t.get("close_timestamp"))):
            tid = _int(t.get("trade_id"))
            if tid in self._closed:
                continue
            if any(o.get("is_open") for o in (t.get("orders") or []) if isinstance(o, dict)):
                continue  # у закрытой сделки ещё есть открытый ордер — дождёмся его судьбы
            self._closed.add(tid)
            st["trade_assigned"].pop(str(tid), None)
            # записи ордеров завершённой сделки больше не нужны для идемпотентности (сделку целиком пропускаем)
            prefix = f"{tid}:"
            for key in [k for k in st["orders"] if k.startswith(prefix)]:
                st["orders"].pop(key, None)
            st["funding_closed"] = _num(st["funding_closed"]) + _num(t.get("funding_fees"))
            symbol = pair_to_symbol(str(t.get("pair") or ""))
            side = "short" if t.get("is_short") else "long"
            pct = _num(t.get("profit_pct"))
            msg = (f"закрыта сделка #{tid} {t.get('pair')} {side}: {_num(t.get('profit_abs')):+.4f} "
                   f"{t.get('quote_currency') or ''} ({pct:+.2f}%), выход: {t.get('exit_reason') or '?'}, "
                   f"вход: {t.get('enter_tag') or ''}")
            self.store.event(_int(t.get("close_timestamp")) or now_ms(), "info", msg, symbol)
            n += 1
        return n

    # ---------------------------------------------------------------- equity / позиции
    @staticmethod
    def _trade_mark(t: dict) -> float:
        return _num(t.get("current_rate")) or _num(t.get("close_rate")) or _num(t.get("open_rate"))

    def _write_equity(self, ts: int, profit: dict, open_trades: list[dict]) -> dict:
        st = self.state
        initial = _num(st["initial_equity"])
        profit_all = _num(profit.get("profit_all_coin"))
        equity = initial + profit_all
        funding = _num(st["funding_closed"]) + sum(_num(t.get("funding_fees")) for t in open_trades)
        realized = _num(st["realized_total"])
        fees = _num(st["fees_total"])
        # тождество hope: equity = initial + realized - fees + funding + unrealized
        unrealized = equity - initial - realized + fees - funding
        active = [t for t in open_trades if _num(t.get("amount")) > _EPS]
        gross_notional = sum(_num(t.get("amount")) * self._trade_mark(t) for t in active)
        n_open_orders = sum(1 for t in open_trades for o in (t.get("orders") or []) if o.get("is_open"))
        peak = max(_num(st["peak_equity"]) or initial, equity)
        st["peak_equity"] = peak
        st["max_drawdown"] = max(_num(st["max_drawdown"]), peak - equity)
        snap = {
            "equity": equity,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "fees": fees,
            "funding": funding,
            "gross_notional": gross_notional,
            "n_positions": len(active),
            "n_fills": int(st["n_fills"]),
            "max_drawdown": _num(st["max_drawdown"]),
        }
        self.store.equity(ts, snap, n_open_orders)
        st["last_equity_ts"] = ts
        return snap

    def _write_positions(self, ts: int, open_trades: list[dict]) -> None:
        st = self.state
        now_open: list[str] = []
        for t in open_trades:
            amount = _num(t.get("amount"))
            if amount <= _EPS:
                continue
            symbol = pair_to_symbol(str(t.get("pair") or ""))
            base = self._positions.get(symbol) or _Pos(symbol)
            mark = self._trade_mark(t)
            row = _Pos(
                symbol=symbol,
                qty=-amount if t.get("is_short") else amount,
                avg_price=_num(t.get("open_rate")),
                realized_pnl=base.realized_pnl,
                fees=base.fees,
                funding=_num(t.get("funding_fees")),
                n_fills=base.n_fills,
                opened_ts=_int(t.get("open_fill_timestamp")) or _int(t.get("open_timestamp")),
                mark=mark,
                unrealized_pnl=_num(t.get("profit_abs")),
            )
            if symbol in self._positions:
                self._positions[symbol].mark = mark
            self.store.position(ts, row)
            now_open.append(symbol)
        for symbol in st.get("open_symbols", []):
            if symbol in now_open:
                continue
            base = self._positions.get(symbol) or _Pos(symbol)
            row = _Pos(symbol=symbol, qty=0.0, avg_price=0.0, realized_pnl=base.realized_pnl, fees=base.fees,
                       funding=0.0, n_fills=base.n_fills, opened_ts=0, mark=base.mark, unrealized_pnl=0.0)
            self.store.position(ts, row)
        st["open_symbols"] = now_open

    # ---------------------------------------------------------------- события связи
    def _on_connected(self, cfg: dict) -> None:
        if self._connected:
            return
        self._connected = True
        msg = (f"подключение к Freqtrade {self.url}: бот {cfg.get('bot_name')}, стратегия {cfg.get('strategy')}, "
               f"v{cfg.get('version')}, {cfg.get('exchange')} {cfg.get('trading_mode')}, dry_run={cfg.get('dry_run')}")
        log.info(msg)
        self.store.event(now_ms(), "info", msg)

    def _on_error(self, err: BaseException) -> None:
        if self._connected:
            self._connected = False
            msg = f"потеряно соединение с Freqtrade {self.url}: {err}"
            log.warning(msg)
            if self.store.run_id:
                self.store.event(now_ms(), "warning", msg)
        else:
            log.warning("Freqtrade %s недоступен: %s", self.url, err)

    # ---------------------------------------------------------------- цикл
    def poll_once(self) -> dict:
        """Один цикл: прочитать API, дописать новое в БД, сохранить состояние. Бросает FreqtradeApiError."""
        cfg = self.client.show_config()
        self._ensure_run(cfg)
        open_trades = self.client.status()
        closed = self._fetch_closed()
        profit = self.client.profit()
        # ниже только запись — все данные уже получены
        self._on_connected(cfg)
        now = now_ms()
        n_new = self._export_fills(closed, open_trades, now)
        n_closed = self._finalize_closed(closed)
        snap = self._write_equity(now, profit, open_trades)
        self._write_positions(now, open_trades)
        self.store.flush()
        self._save_state()
        log.debug("опрос: новых исполнений %d, закрытых сделок %d, equity %.2f, открытых %d",
                  n_new, n_closed, snap["equity"], snap["n_positions"])
        return {"run_id": self.store.run_id, "n_new_fills": n_new, "n_new_closed": n_closed,
                "n_open": len(open_trades), "equity": snap["equity"]}

    async def run(self, stop_event: asyncio.Event | None = None, once: bool = False) -> None:
        """Опрашивать до stop_event (или один раз при once=True); ошибки API — лог и бэкофф."""
        stop_event = stop_event or asyncio.Event()
        backoff = self.interval
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(self.poll_once)
                backoff = self.interval
            except FreqtradeApiError as e:
                self._on_error(e)
                backoff = min(max(backoff, 1.0) * 2, MAX_BACKOFF_SECS)
            except Exception as e:  # экспортёр не должен падать из-за одной ошибки
                log.exception("ошибка опроса")
                self._on_error(e)
                backoff = min(max(backoff, 1.0) * 2, MAX_BACKOFF_SECS)
            if once:
                break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except TimeoutError:
                pass
        if not once and stop_event.is_set():
            self.finish("stopped")

    def summary(self) -> dict:
        st = self.state
        return {
            "n_fills": int(st["n_fills"]),
            "n_closed_trades": len(self._closed),
            "realized_pnl": _num(st["realized_total"]),
            "fees": _num(st["fees_total"]),
            "funding_closed": _num(st["funding_closed"]),
            "max_drawdown": _num(st["max_drawdown"]),
            "last_equity_ts": int(st.get("last_equity_ts") or 0),
        }

    def finish(self, status: str = "stopped") -> None:
        """Отметить запуск завершённым (только по явной остановке экспортёра)."""
        if self.store.run_id and not self._closed_flag:
            try:
                self.store.finish_run(status, self.summary())
                log.info("запуск run_id=%s отмечен как %s", self.store.run_id, status)
            except Exception as e:  # noqa: BLE001
                log.error("не удалось завершить запуск: %s", e)
        self._save_state()

    def close(self) -> None:
        if self._closed_flag:
            return
        self._closed_flag = True
        try:
            self.client.close()
        finally:
            self.store.close()
