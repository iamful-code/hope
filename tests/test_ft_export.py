"""Тесты экспортёра Freqtrade -> hope на фикстурах REST API (httpx.MockTransport, без сети).

Сценарий «бота»: фаза 1 — две закрытые сделки (long BTC с одним выходом, short ETH с двумя частичными
выходами) и одна открытая (long SOL с открытым лимитным ордером на выход); фаза 2 — SOL закрылась.
Числа подобраны по формулам Freqtrade: profit_abs = close_value(1 - fee_close) - open_value(1 + fee_open) + funding.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

import httpx
import pytest

from hope.adapters.freqtrade import exporter as ex
from hope.adapters.freqtrade.exporter import FreqtradeApiError, FreqtradeExporter, pair_to_symbol

T0 = 1_726_000_000_000  # мс
FEE_M, FEE_T = 0.0002, 0.00055  # maker / taker Bybit


# ====================================================================== фикстуры JSON по схемам Freqtrade
def _order(order_id, side, is_entry, amount, price, filled_ts, *, pair, order_type="limit", status="closed",
           is_open=False, tag=None, placed_ts=None):
    filled = 0.0 if is_open else amount
    return {
        "pair": pair, "order_id": order_id, "status": status, "remaining": amount - filled, "amount": amount,
        "safe_price": price, "cost": amount * price, "filled": filled, "ft_order_side": side,
        "order_type": order_type, "is_open": is_open, "order_timestamp": placed_ts or (filled_ts - 30_000),
        "order_filled_timestamp": None if is_open else filled_ts, "ft_fee_base": None, "ft_order_tag": tag,
        "ft_is_entry": is_entry, "average": None if is_open else price, "price": price, "funding_fee": None,
        "order_date": "2024-09-10 20:00:00", "order_filled_date": None if is_open else "2024-09-10 20:01:00",
    }


def _trade(trade_id, pair, is_short, amount, open_rate, orders, *, is_open, open_ts, close_ts=None, close_rate=None,
           profit_abs=None, fee_open=FEE_M, fee_close=FEE_T, funding=0.0, enter_tag="sig", exit_reason=None,
           current_rate=None):
    stake = amount * open_rate
    ratio = (profit_abs or 0.0) / stake
    d = {
        "trade_id": trade_id, "pair": pair, "base_currency": pair.split("/")[0], "quote_currency": "USDT",
        "is_open": is_open, "is_short": is_short, "exchange": "bybit", "amount": amount, "amount_requested": amount,
        "stake_amount": stake, "max_stake_amount": stake, "strategy": "SampleStrategy", "enter_tag": enter_tag,
        "timeframe": 5, "fee_open": fee_open, "fee_open_cost": stake * fee_open, "fee_open_currency": "USDT",
        "fee_close": fee_close, "fee_close_cost": amount * close_rate * fee_close if close_rate else None,
        "fee_close_currency": "USDT", "open_date": "2024-09-10 20:00:00", "open_timestamp": open_ts,
        "open_fill_date": "2024-09-10 20:01:00", "open_fill_timestamp": orders[0]["order_filled_timestamp"],
        "open_rate": open_rate, "open_rate_requested": open_rate,
        "open_trade_value": stake * (1 - fee_open if is_short else 1 + fee_open),
        "close_date": None if is_open else "2024-09-10 21:00:00", "close_timestamp": close_ts,
        "close_rate": close_rate, "close_rate_requested": close_rate,
        "close_profit": None if is_open else ratio, "close_profit_pct": None if is_open else round(ratio * 100, 2),
        "close_profit_abs": None if is_open else profit_abs, "trade_duration_s": None, "trade_duration": None,
        "profit_ratio": ratio, "profit_pct": round(ratio * 100, 2), "profit_abs": profit_abs, "profit_fiat": None,
        "realized_profit": 0.0, "realized_profit_ratio": None, "exit_reason": exit_reason,
        "exit_order_status": None if is_open else "closed",
        "stop_loss_abs": open_rate * (1.05 if is_short else 0.95), "stop_loss_ratio": -0.05, "stop_loss_pct": -5.0,
        "stoploss_last_update": None, "stoploss_last_update_timestamp": None,
        "initial_stop_loss_abs": open_rate * (1.05 if is_short else 0.95), "initial_stop_loss_ratio": -0.05,
        "initial_stop_loss_pct": -5.0, "min_rate": open_rate * 0.99, "max_rate": open_rate * 1.02, "leverage": 1.0,
        "interest_rate": 0.0, "liquidation_price": None, "funding_fees": funding, "trading_mode": "futures",
        "amount_precision": 3, "price_precision": 2, "precision_mode": 4, "precision_mode_price": 4,
        "contract_size": 1, "nr_of_successful_entries": 1,
        "nr_of_successful_exits": sum(1 for o in orders if not o["ft_is_entry"] and not o["is_open"]),
        "has_open_orders": any(o["is_open"] for o in orders), "orders": orders,
    }
    if is_open:
        d.update({
            "current_rate": current_rate, "total_profit_abs": profit_abs, "total_profit_fiat": None,
            "total_profit_ratio": ratio, "stoploss_current_dist": -7.5, "stoploss_current_dist_pct": -5.0,
            "stoploss_current_dist_ratio": -0.05, "stoploss_entry_dist": -7.5, "stoploss_entry_dist_ratio": -0.05,
            "open_orders": "(limit sell rem=1.00000000)",
        })
    return d


BTC, ETH, SOL = "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"

# long BTC 0.002 @ 50000 -> 51000 (limit вход maker, market выход taker), фандинг +0.01
T1_GROSS, T1_FEES, T1_FUND = 2.0, 0.002 * 50000 * FEE_M + 0.002 * 51000 * FEE_T, 0.01
T1 = _trade(
    1, BTC, False, 0.002, 50000.0,
    [_order("dry_run_buy_1", "buy", True, 0.002, 50000.0, T0 + 120_000, pair=BTC, tag="ema_cross"),
     _order("dry_run_sell_2", "sell", False, 0.002, 51000.0, T0 + 600_000, pair=BTC, order_type="market")],
    is_open=False, open_ts=T0 + 60_000, close_ts=T0 + 600_000, close_rate=51000.0,
    profit_abs=T1_GROSS - T1_FEES + T1_FUND, funding=T1_FUND, enter_tag="ema_cross", exit_reason="roi",
)
# short ETH 0.05 @ 3000, два частичных выхода 0.02 @ 3020 и 0.03 @ 3040 (убыток)
T2_GROSS = 0.05 * 3000 - (0.02 * 3020 + 0.03 * 3040)
T2_FEES = 0.05 * 3000 * FEE_M + 0.02 * 3020 * FEE_T + 0.03 * 3040 * FEE_T
T2 = _trade(
    2, ETH, True, 0.05, 3000.0,
    [_order("dry_run_sell_3", "sell", True, 0.05, 3000.0, T0 + 130_000, pair=ETH, tag="short_sig"),
     _order("dry_run_buy_4", "buy", False, 0.02, 3020.0, T0 + 400_000, pair=ETH, order_type="market", tag="partial_exit"),
     _order("dry_run_buy_5", "buy", False, 0.03, 3040.0, T0 + 700_000, pair=ETH, order_type="market")],
    is_open=False, open_ts=T0 + 100_000, close_ts=T0 + 700_000, close_rate=3032.0,
    profit_abs=T2_GROSS - T2_FEES, enter_tag="short_sig", exit_reason="stop_loss",
)
# long SOL 1.0 @ 150: фаза 1 открыта (mark 152, открыт лимитный выход @153), фаза 2 закрыта @153 (maker)
SOL_ENTRY = _order("dry_run_buy_6", "buy", True, 1.0, 150.0, T0 + 830_000, pair=SOL, tag="ema_cross")
T3_OPEN_PNL = 152 * (1 - FEE_M) - 150 * (1 + FEE_M)
T3_OPEN = _trade(
    3, SOL, False, 1.0, 150.0,
    [SOL_ENTRY, _order("dry_run_sell_7", "sell", False, 1.0, 153.0, T0 + 900_000, pair=SOL, status="open",
                       is_open=True)],
    is_open=True, open_ts=T0 + 800_000, fee_close=FEE_M, enter_tag="ema_cross", current_rate=152.0,
    profit_abs=T3_OPEN_PNL,
)
T3_GROSS, T3_FEES = 3.0, 150 * FEE_M + 153 * FEE_M
T3_CLOSED = _trade(
    3, SOL, False, 1.0, 150.0,
    [SOL_ENTRY, _order("dry_run_sell_7", "sell", False, 1.0, 153.0, T0 + 1_000_000, pair=SOL,
                       placed_ts=T0 + 900_000)],
    is_open=False, open_ts=T0 + 800_000, close_ts=T0 + 1_000_000, close_rate=153.0,
    profit_abs=T3_GROSS - T3_FEES, fee_close=FEE_M, enter_tag="ema_cross", exit_reason="exit_signal",
)

BALANCE = {
    "currencies": [{"currency": "USDT", "free": 9700.0, "balance": 10000.0, "used": 300.0, "bot_owned": 9700.0,
                    "est_stake": 10000.0, "est_stake_bot": 10000.0, "stake": "USDT", "side": "long",
                    "is_position": False, "position": 0, "is_bot_managed": True}],
    "total": 10002.0, "total_bot": 10002.0, "symbol": "USD", "value": 10002.0, "value_bot": 10002.0, "stake": "USDT",
    "note": "", "starting_capital": 10000.0, "starting_capital_ratio": 0.0002, "starting_capital_pct": 0.02,
    "starting_capital_fiat": 10000.0, "starting_capital_fiat_ratio": 0.0002, "starting_capital_fiat_pct": 0.02,
}
WHITELIST = {"method": ["StaticPairList"], "length": 3, "whitelist": [BTC, ETH, SOL]}


def _show_config(strategy: str) -> dict:
    return {
        "version": "2026.8", "strategy_version": None, "api_version": 2.43, "dry_run": True,
        "trading_mode": "futures", "margin_mode": "isolated", "short_allowed": True, "stake_currency": "USDT",
        "proxy_coin": None, "stake_amount": "100", "available_capital": 10000, "stake_currency_decimals": 3,
        "max_open_trades": 5, "minimal_roi": {"0": 0.02}, "stoploss": -0.05, "stoploss_on_exchange": False,
        "trailing_stop": False, "trailing_stop_positive": None, "trailing_stop_positive_offset": 0.0,
        "trailing_only_offset_is_reached": False,
        "unfilledtimeout": {"entry": 10, "exit": 10, "unit": "minutes", "exit_timeout_count": 0},
        "order_types": {"entry": "limit", "exit": "limit", "stoploss": "market", "stoploss_on_exchange": False},
        "use_custom_stoploss": False, "timeframe": "5m", "timeframe_ms": 300000, "timeframe_min": 5,
        "exchange": "bybit", "demo_trading": False, "strategy": strategy, "force_entry_enable": False,
        "exit_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
        "entry_pricing": {"price_side": "same", "use_order_book": True, "order_book_top": 1},
        "bot_name": "hope-ft", "state": "running", "runmode": "dry_run", "position_adjustment_enable": False,
        "max_entry_position_adjustment": -1,
    }


def _profit(closed: float, all_: float, n_closed: int, n_all: int) -> dict:
    return {
        "profit_closed_coin": closed, "profit_closed_percent_mean": 0.0, "profit_closed_ratio_mean": 0.0,
        "profit_closed_percent_sum": 0.0, "profit_closed_ratio_sum": 0.0, "profit_closed_percent": 0.0,
        "profit_closed_ratio": closed / 10000, "profit_closed_fiat": closed, "profit_all_coin": all_,
        "profit_all_percent_mean": 0.0, "profit_all_ratio_mean": 0.0, "profit_all_percent_sum": 0.0,
        "profit_all_ratio_sum": 0.0, "profit_all_percent": 0.0, "profit_all_ratio": all_ / 10000,
        "profit_all_fiat": all_, "trade_count": n_all, "closed_trade_count": n_closed,
        "first_trade_date": "", "first_trade_humanized": "", "first_trade_timestamp": T0 + 60_000,
        "latest_trade_date": "", "latest_trade_humanized": "", "latest_trade_timestamp": T0 + 800_000,
        "avg_duration": "0:10:00", "best_pair": BTC, "best_rate": 1.93, "best_pair_profit_ratio": 0.0193,
        "best_pair_profit_abs": 1.93, "winning_trades": 1, "losing_trades": 1, "profit_factor": 1.1,
        "winrate": 0.5, "expectancy": 0.1, "expectancy_ratio": 0.01, "sharpe": 0.0, "sortino": 0.0, "sqn": 0.0,
        "calmar": 0.0, "cagr": 0.0, "max_drawdown": 0.0, "max_drawdown_abs": 1.7, "max_drawdown_start": "",
        "max_drawdown_start_timestamp": 0, "max_drawdown_end": "", "max_drawdown_end_timestamp": 0,
        "current_drawdown": 0.0, "current_drawdown_abs": 0.0, "current_drawdown_high": 0.0,
        "current_drawdown_start": "", "current_drawdown_start_timestamp": 0, "trading_volume": 500.0,
        "bot_start_timestamp": T0, "bot_start_date": "2024-09-10 19:00:00",
    }


class FakeBot:
    """REST API Freqtrade на MockTransport: фазы 1/2, отказ /trades, полная недоступность."""

    def __init__(self) -> None:
        self.phase = 1
        self.strategy = "SampleStrategy"
        self.fail_trades = False
        self.down = False
        self.calls: Counter[str] = Counter()

    def closed(self) -> list[dict]:
        return [T1, T2] + ([T3_CLOSED] if self.phase >= 2 else [])

    def open(self) -> list[dict]:
        return [T3_OPEN] if self.phase == 1 else []

    def profit(self) -> dict:
        closed = T1["profit_abs"] + T2["profit_abs"] + (T3_CLOSED["profit_abs"] if self.phase >= 2 else 0.0)
        all_ = closed + (T3_OPEN_PNL if self.phase == 1 else 0.0)
        return _profit(closed, all_, len(self.closed()), 3)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        assert request.headers.get("authorization", "").startswith("Basic ")
        path = request.url.path
        self.calls[path] += 1
        if path == "/api/v1/ping":
            return httpx.Response(200, json={"status": "pong"})
        if path == "/api/v1/show_config":
            return httpx.Response(200, json=_show_config(self.strategy))
        if path == "/api/v1/balance":
            return httpx.Response(200, json=BALANCE)
        if path == "/api/v1/whitelist":
            return httpx.Response(200, json=WHITELIST)
        if path == "/api/v1/status":
            return httpx.Response(200, json=self.open())
        if path == "/api/v1/profit":
            return httpx.Response(200, json=self.profit())
        if path == "/api/v1/trades":
            if self.fail_trades:
                return httpx.Response(500, text="Internal Server Error")
            limit = int(request.url.params.get("limit", 500))
            offset = int(request.url.params.get("offset", 0))
            assert request.url.params.get("order_by_id") == "false"
            closed = sorted(self.closed(), key=lambda t: -t["close_timestamp"])
            page = closed[offset:offset + limit]
            return httpx.Response(200, json={"trades": page, "trades_count": len(page), "offset": offset,
                                             "total_trades": len(closed)})
        return httpx.Response(404, json={"detail": "Not Found"})


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


def _exporter(tmp_path: Path, bot: FakeBot, **kw) -> FreqtradeExporter:
    return FreqtradeExporter("http://ft.test:8080/", "u", "p", tmp_path / "hope.db", interval_secs=0.01,
                             transport=httpx.MockTransport(bot.handle), **kw)


def _rows(db: Path, sql: str, *args) -> list[dict]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


# ====================================================================== тесты
def test_pair_to_symbol():
    assert pair_to_symbol("BTC/USDT:USDT") == "BTCUSDT"
    assert pair_to_symbol("1000PEPE/USDT:USDT") == "1000PEPEUSDT"
    assert pair_to_symbol("ETH/USDT") == "ETHUSDT"


def test_two_polls_export(tmp_path, bot, monkeypatch):
    monkeypatch.setattr(ex, "PAGE_SIZE", 2)  # проверяем пагинацию /trades
    db = tmp_path / "hope.db"
    e = _exporter(tmp_path, bot)
    try:
        r = e.poll_once()
        assert r["n_new_fills"] == 6 and r["n_new_closed"] == 2 and r["n_open"] == 1

        # ---- запуск
        runs = _rows(db, "SELECT * FROM runs")
        assert len(runs) == 1
        run = runs[0]
        assert run["engine"] == "freqtrade" and run["strategy"] == "SampleStrategy" and run["mode"] == "live"
        assert run["status"] == "running" and run["finished_ts"] is None and run["started_ts"] == T0
        assert run["initial_equity"] == 10000.0 and json.loads(run["symbols"]) == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
        assert json.loads(run["config_json"])["bot_name"] == "hope-ft" and run["name"] == "ft:SampleStrategy:hope-ft"
        assert json.loads(run["params_json"])["dry_run"] is True
        run_id = run["run_id"]

        # ---- исполнения: в хронологическом порядке по всем сделкам, seq сквозной
        fills = _rows(db, "SELECT * FROM fills WHERE run_id=? ORDER BY seq", run_id)
        assert [f["seq"] for f in fills] == [1, 2, 3, 4, 5, 6]
        assert [f["ts"] for f in fills] == sorted(f["ts"] for f in fills)
        assert [f["symbol"] for f in fills] == ["BTCUSDT", "ETHUSDT", "ETHUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"]
        assert [f["side"] for f in fills] == ["Buy", "Sell", "Buy", "Sell", "Buy", "Buy"]
        assert [f["purpose"] for f in fills] == ["entry", "entry", "exit", "exit", "exit", "entry"]
        assert [f["position_after"] for f in fills] == pytest.approx([0.002, -0.05, -0.03, 0.0, 0.0, 1.0])
        assert [f["inventory_before"] for f in fills] == pytest.approx([0.0, 0.0, -0.05, 0.002, -0.03, 0.0])
        assert [f["is_maker"] for f in fills] == [1, 1, 0, 0, 0, 1]
        assert [f["tag"] for f in fills] == ["ema_cross", "short_sig", "partial_exit", "roi", "stop_loss", "ema_cross"]
        assert [f["qty"] for f in fills] == pytest.approx([0.002, 0.05, 0.02, 0.002, 0.03, 1.0])
        assert [f["price"] for f in fills] == pytest.approx([50000, 3000, 3020, 51000, 3040, 150])
        # комиссии: вход по fee_open (maker), выход по fee_close (taker)
        assert fills[0]["fee"] == pytest.approx(0.002 * 50000 * FEE_M)
        assert fills[3]["fee"] == pytest.approx(0.002 * 51000 * FEE_T)
        assert fills[2]["fee"] + fills[4]["fee"] + fills[1]["fee"] == pytest.approx(T2_FEES)
        # realized: грязный PnL сделки = profit_abs + комиссии - фандинг, по выходам пропорционально объёму
        assert fills[3]["realized_pnl"] == pytest.approx(T1["profit_abs"] + T1_FEES - T1_FUND) == pytest.approx(2.0)
        assert fills[2]["realized_pnl"] == pytest.approx(T2_GROSS * 0.02 / 0.05)
        assert fills[4]["realized_pnl"] == pytest.approx(T2_GROSS * 0.03 / 0.05)
        assert fills[2]["realized_pnl"] + fills[4]["realized_pnl"] == pytest.approx(T2["profit_abs"] + T2_FEES)
        assert fills[0]["realized_pnl"] == fills[1]["realized_pnl"] == fills[5]["realized_pnl"] == 0.0
        assert fills[5]["placed_ts"] == SOL_ENTRY["order_timestamp"]

        # ---- ордера: целочисленные id, те же, что у исполнений
        orders = _rows(db, "SELECT * FROM orders WHERE run_id=? ORDER BY order_id", run_id)
        assert len(orders) == 6 and {o["status"] for o in orders} == {"filled"}
        assert len({o["order_id"] for o in orders}) == 6 and all(isinstance(o["order_id"], int) for o in orders)
        assert {f["order_id"] for f in fills} == {o["order_id"] for o in orders}
        assert [o["taker"] for o in sorted(orders, key=lambda o: o["ts_done"])] == [0, 0, 1, 1, 1, 0]

        # ---- equity
        eq = _rows(db, "SELECT * FROM equity WHERE run_id=? ORDER BY ts", run_id)
        assert len(eq) == 1
        s = eq[0]
        profit_all = T1["profit_abs"] + T2["profit_abs"] + T3_OPEN_PNL
        assert s["equity"] == pytest.approx(10000 + profit_all)
        assert s["realized_pnl"] == pytest.approx(T1_GROSS + T2_GROSS)
        fees_all = T1_FEES + T2_FEES + 150 * FEE_M
        assert s["fees"] == pytest.approx(fees_all) and s["fees"] == pytest.approx(sum(f["fee"] for f in fills))
        assert s["funding"] == pytest.approx(T1_FUND)
        assert s["unrealized_pnl"] == pytest.approx(profit_all - (T1_GROSS + T2_GROSS) + fees_all - T1_FUND)
        assert s["n_positions"] == 1 and s["gross_notional"] == pytest.approx(152.0) and s["n_fills"] == 6
        assert s["n_open_orders"] == 1 and s["max_drawdown"] == 0.0

        # ---- позиции
        pos = _rows(db, "SELECT * FROM positions WHERE run_id=?", run_id)
        assert len(pos) == 1
        p = pos[0]
        assert p["symbol"] == "SOLUSDT" and p["qty"] == 1.0 and p["avg_price"] == 150.0 and p["mark"] == 152.0
        assert p["unrealized_pnl"] == pytest.approx(T3_OPEN_PNL) and p["n_fills"] == 1
        assert p["fees"] == pytest.approx(150 * FEE_M) and p["opened_ts"] == T0 + 830_000

        # ---- события: подключение + две закрытые сделки
        ev = _rows(db, "SELECT * FROM events WHERE run_id=? ORDER BY ts", run_id)
        assert [x["level"] for x in ev].count("info") == 3 and len(ev) == 3
        closed_ev = [x for x in ev if "закрыта сделка" in x["msg"]]
        assert [x["ts"] for x in closed_ev] == [T0 + 600_000, T0 + 700_000]
        assert closed_ev[0]["symbol"] == "BTCUSDT" and "#1 BTC/USDT:USDT long" in closed_ev[0]["msg"]
        assert "roi" in closed_ev[0]["msg"] and "short" in closed_ev[1]["msg"] and "stop_loss" in closed_ev[1]["msg"]
        assert any("подключение" in x["msg"] for x in ev)

        # ---- состояние
        st = json.loads((tmp_path / "hope.db.ftexport.json").read_text(encoding="utf-8"))
        assert st["run_id"] == run_id and st["seq"] == 6 and st["open_symbols"] == ["SOLUSDT"]
        assert sorted(st["closed_trades"]) == [1, 2]
        assert set(st["orders"]) == {"3:dry_run_buy_6"}  # ордера закрытых сделок вычищены

        # ================== фаза 2: SOL закрылась
        bot.phase = 2
        r = e.poll_once()
        assert r["n_new_fills"] == 1 and r["n_new_closed"] == 1 and r["n_open"] == 0
        fills = _rows(db, "SELECT * FROM fills WHERE run_id=? ORDER BY seq", run_id)
        assert len(fills) == 7 and [f["seq"] for f in fills] == list(range(1, 8))
        last = fills[-1]
        assert last["symbol"] == "SOLUSDT" and last["side"] == "Sell" and last["purpose"] == "exit"
        assert last["price"] == 153.0 and last["qty"] == 1.0 and last["is_maker"] == 1 and last["tag"] == "exit_signal"
        assert last["realized_pnl"] == pytest.approx(T3_GROSS) and last["position_after"] == 0.0
        assert last["inventory_before"] == 1.0 and last["fee"] == pytest.approx(153 * FEE_M)
        assert last["ts"] == T0 + 1_000_000 and last["placed_ts"] == T0 + 900_000
        assert len(_rows(db, "SELECT * FROM orders WHERE run_id=?", run_id)) == 7

        eq = _rows(db, "SELECT * FROM equity WHERE run_id=? ORDER BY ts", run_id)
        assert len(eq) == 2
        s = eq[-1]
        profit_all = T1["profit_abs"] + T2["profit_abs"] + T3_CLOSED["profit_abs"]
        assert s["equity"] == pytest.approx(10000 + profit_all)
        assert s["realized_pnl"] == pytest.approx(T1_GROSS + T2_GROSS + T3_GROSS)
        assert s["fees"] == pytest.approx(T1_FEES + T2_FEES + T3_FEES)
        assert s["unrealized_pnl"] == pytest.approx(0.0, abs=1e-9)
        assert s["n_positions"] == 0 and s["gross_notional"] == 0.0 and s["n_open_orders"] == 0 and s["n_fills"] == 7

        pos = {p["symbol"]: p for p in _rows(db, "SELECT * FROM positions WHERE run_id=?", run_id)}
        assert pos["SOLUSDT"]["qty"] == 0.0 and pos["SOLUSDT"]["n_fills"] == 2  # плоская строка
        assert pos["SOLUSDT"]["fees"] == pytest.approx(T3_FEES)

        ev = _rows(db, "SELECT * FROM events WHERE run_id=? ORDER BY ts", run_id)
        closed_ev = [x for x in ev if "закрыта сделка" in x["msg"]]
        assert len(ev) == 4 and len(closed_ev) == 3
        assert "#3 SOL/USDT:USDT long" in closed_ev[-1]["msg"] and "exit_signal" in closed_ev[-1]["msg"]
        assert closed_ev[-1]["ts"] == T0 + 1_000_000 and closed_ev[-1]["symbol"] == "SOLUSDT"

        # ================== фаза 2 ещё раз: идемпотентность
        r = e.poll_once()
        assert r["n_new_fills"] == 0 and r["n_new_closed"] == 0
        assert len(_rows(db, "SELECT * FROM fills WHERE run_id=?", run_id)) == 7
        assert len(_rows(db, "SELECT * FROM orders WHERE run_id=?", run_id)) == 7
        assert len(_rows(db, "SELECT * FROM events WHERE run_id=?", run_id)) == 4
        assert len(_rows(db, "SELECT * FROM equity WHERE run_id=?", run_id)) == 3
        assert _rows(db, "SELECT * FROM positions WHERE run_id=?", run_id)[0]["qty"] == 0.0
        # пагинация при PAGE_SIZE=2: фаза 1 — 2 страницы, фаза 2 — 2, повтор — 1 (все известны)
        assert bot.calls["/api/v1/trades"] == 5
        assert bot.calls["/api/v1/balance"] == 1 and bot.calls["/api/v1/whitelist"] == 1

        e.finish("stopped")
        run = _rows(db, "SELECT * FROM runs")[0]
        assert run["status"] == "stopped" and run["finished_ts"] is not None
        assert json.loads(run["summary_json"])["n_fills"] == 7
    finally:
        e.close()


def test_restart_continues_same_run(tmp_path, bot):
    db = tmp_path / "hope.db"
    e1 = _exporter(tmp_path, bot)
    e1.poll_once()
    run_id = e1.store.run_id
    e1.finish("stopped")
    e1.close()
    assert _rows(db, "SELECT status FROM runs")[0]["status"] == "stopped"

    # рестарт: тот же файл состояния -> тот же run_id, статус снова running, исполнения не дублируются
    bot.phase = 2
    e2 = _exporter(tmp_path, bot)
    try:
        r = e2.poll_once()
        assert e2.store.run_id == run_id and r["n_new_fills"] == 1
        runs = _rows(db, "SELECT * FROM runs")
        assert len(runs) == 1 and runs[0]["status"] == "running" and runs[0]["finished_ts"] is None
        fills = _rows(db, "SELECT * FROM fills WHERE run_id=? ORDER BY seq", run_id)
        assert len(fills) == 7 and fills[-1]["seq"] == 7 and fills[-1]["position_after"] == 0.0
        assert len({f["order_id"] for f in fills}) == 7
    finally:
        e2.close()

    # смена стратегии у бота -> новый запуск, старый не трогаем
    bot.strategy = "OtherStrategy"
    e3 = _exporter(tmp_path, bot, run_name="ft-other")
    try:
        e3.poll_once()
        runs = _rows(db, "SELECT * FROM runs ORDER BY run_id")
        assert len(runs) == 2 and runs[1]["strategy"] == "OtherStrategy" and runs[1]["name"] == "ft-other"
        assert e3.store.run_id == runs[1]["run_id"] != run_id
        assert len(_rows(db, "SELECT * FROM fills WHERE run_id=?", run_id)) == 7
        assert len(_rows(db, "SELECT * FROM fills WHERE run_id=?", e3.store.run_id)) == 7
        st = json.loads((tmp_path / "hope.db.ftexport.json").read_text(encoding="utf-8"))
        assert st["run_id"] == e3.store.run_id and st["strategy"] == "OtherStrategy"
    finally:
        e3.close()


def test_custom_state_path_and_name(tmp_path, bot):
    e = _exporter(tmp_path, bot, run_name="мой-бот", state_path=tmp_path / "st.json")
    try:
        e.poll_once()
        assert (tmp_path / "st.json").exists() and not (tmp_path / "hope.db.ftexport.json").exists()
        assert _rows(tmp_path / "hope.db", "SELECT name FROM runs")[0]["name"] == "мой-бот"
    finally:
        e.close()


async def test_transient_errors_do_not_crash(tmp_path, bot):
    db = tmp_path / "hope.db"
    e = _exporter(tmp_path, bot)
    try:
        e.poll_once()
        run_id = e.store.run_id
        bot.fail_trades = True
        with pytest.raises(FreqtradeApiError):
            e.poll_once()
        await e.run(once=True)  # ошибка залогирована, событие записано, исключения нет
        e.store.flush()
        ev = _rows(db, "SELECT * FROM events WHERE run_id=? ORDER BY ts, rowid", run_id)
        assert ev[-1]["level"] == "warning" and "потеряно соединение" in ev[-1]["msg"]
        assert len(_rows(db, "SELECT * FROM fills WHERE run_id=?", run_id)) == 6
        assert len(_rows(db, "SELECT * FROM equity WHERE run_id=?", run_id)) == 1

        bot.fail_trades = False
        await e.run(once=True)
        ev = _rows(db, "SELECT * FROM events WHERE run_id=? ORDER BY ts, rowid", run_id)
        assert sum(1 for x in ev if "подключение" in x["msg"]) == 2
        assert len(_rows(db, "SELECT * FROM equity WHERE run_id=?", run_id)) == 2
        assert _rows(db, "SELECT status FROM runs")[0]["status"] == "running"  # once не завершает запуск
    finally:
        e.close()


async def test_bot_down_before_first_run(tmp_path, bot):
    bot.down = True
    e = _exporter(tmp_path, bot)
    try:
        with pytest.raises(FreqtradeApiError):
            e.poll_once()
        await e.run(once=True)
        assert _rows(tmp_path / "hope.db", "SELECT * FROM runs") == []
        bot.down = False
        await e.run(once=True)
        assert len(_rows(tmp_path / "hope.db", "SELECT * FROM runs")) == 1
    finally:
        e.close()


async def test_run_until_stop_event_marks_stopped(tmp_path, bot):
    import asyncio

    e = _exporter(tmp_path, bot)
    try:
        stop = asyncio.Event()

        async def stopper():
            await asyncio.sleep(0.5)
            stop.set()

        await asyncio.gather(e.run(stop), stopper())
        runs = _rows(tmp_path / "hope.db", "SELECT * FROM runs")
        assert len(runs) == 1 and runs[0]["status"] == "stopped"
        assert len(_rows(tmp_path / "hope.db", "SELECT * FROM equity")) >= 1
    finally:
        e.close()
