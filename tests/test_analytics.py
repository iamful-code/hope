"""Тесты аналитики: FIFO-раунд-трипы на синтетических исполнениях, сводка, кривая equity."""

import math

import pandas as pd
import pytest

from hope.analytics import ROUNDTRIP_COLUMNS, downsample, equity_curve, roundtrips, summary, to_jsonable


def _fill(seq, symbol, side, price, qty, fee, ts, maker, tag, purpose="entry", bid=0.0, ask=0.0):
    return {
        "seq": seq, "fill_id": seq, "order_id": seq, "symbol": symbol, "side": side, "price": price, "qty": qty,
        "fee": fee, "ts": ts, "is_maker": int(maker), "purpose": purpose, "tag": tag, "bid": bid, "ask": ask,
        "realized_pnl": 0.0, "position_after": 0.0, "spread_bps_at_place": 1.0, "queue_ahead_initial": 0.0,
    }


@pytest.fixture
def fills():
    """X: long с двумя частичными выходами, short с переворотом, открытый лот; Y: short; Z: два частичных входа."""
    rows = [
        # ---- X: вход 1.0 long, два частичных выхода
        _fill(1, "X", "Buy", 100.0, 1.0, 0.10, 1000, True, "e1"),
        _fill(2, "X", "Sell", 110.0, 0.4, 0.05, 2000, False, "x1", "exit"),
        _fill(3, "X", "Sell", 90.0, 0.6, 0.06, 3000, True, "x1", "exit"),
        # ---- X: short 2.0, затем переворот покупкой 3.0 (закрывает 2.0, открывает long 1.0)
        _fill(4, "X", "Sell", 100.0, 2.0, 0.20, 4000, True, "s1"),
        _fill(5, "X", "Buy", 95.0, 3.0, 0.30, 5000, False, "flip"),
        _fill(6, "X", "Sell", 105.0, 1.0, 0.10, 8000, True, "x2", "exit"),
        # ---- X: открытый лот в конце
        _fill(7, "X", "Buy", 100.0, 0.5, 0.05, 9000, True, "open"),
        # ---- Y: short раунд-трип в убыток (перемешан по времени с X)
        _fill(8, "Y", "Sell", 50.0, 1.0, 0.01, 1500, True, "ys"),
        _fill(9, "Y", "Buy", 52.0, 1.0, 0.01, 2500, True, "yx", "exit"),
        # ---- Z: два частичных входа, один выход
        _fill(10, "Z", "Buy", 100.0, 0.5, 0.05, 1000, True, "a"),
        _fill(11, "Z", "Buy", 102.0, 0.5, 0.05, 2000, False, "b"),
        _fill(12, "Z", "Sell", 104.0, 1.0, 0.10, 3000, True, "c", "exit"),
    ]
    return pd.DataFrame(rows)


def _rt(df, symbol, i):
    return df[df["symbol"] == symbol].iloc[i]


def test_roundtrips_columns_and_count(fills):
    rt = roundtrips(fills)
    assert list(rt.columns) == ROUNDTRIP_COLUMNS
    assert len(rt) == 6  # X: 4, Y: 1, Z: 1
    assert not rt["open"].any()
    assert rt["close_ts"].is_monotonic_increasing


def test_long_partial_exits(fills):
    rt = roundtrips(fills)
    a = _rt(rt, "X", 0)
    assert a["side"] == "long" and a["qty"] == pytest.approx(0.4)
    assert a["entry_price"] == pytest.approx(100.0) and a["exit_price"] == pytest.approx(110.0)
    assert a["gross_pnl"] == pytest.approx(4.0)
    assert a["fees"] == pytest.approx(0.10 * 0.4 + 0.05)  # комиссия входа пропорционально закрытой части
    assert a["net_pnl"] == pytest.approx(4.0 - 0.09)
    assert a["hold_secs"] == pytest.approx(1.0) and a["n_fills"] == 2
    assert a["entry_tag"] == "e1" and a["exit_tag"] == "x1"
    assert a["maker_share"] == pytest.approx(0.5)  # вход maker, выход taker
    assert a["entry_seq"] == 1 and a["exit_seq"] == 2

    b = _rt(rt, "X", 1)
    assert b["side"] == "long" and b["qty"] == pytest.approx(0.6)
    assert b["gross_pnl"] == pytest.approx(-6.0)
    assert b["fees"] == pytest.approx(0.06 + 0.06)
    assert b["net_pnl"] == pytest.approx(-6.12)
    assert b["maker_share"] == pytest.approx(1.0)
    assert b["open_ts"] == 1000 and b["close_ts"] == 3000


def test_short_and_flip(fills):
    rt = roundtrips(fills)
    c = _rt(rt, "X", 2)  # закрытие short переворотом
    assert c["side"] == "short" and c["qty"] == pytest.approx(2.0)
    assert c["entry_price"] == pytest.approx(100.0) and c["exit_price"] == pytest.approx(95.0)
    assert c["gross_pnl"] == pytest.approx(10.0)
    assert c["fees"] == pytest.approx(0.20 + 0.30 * 2 / 3)  # комиссия переворота — только закрывающая часть
    assert c["net_pnl"] == pytest.approx(9.6)
    assert c["entry_tag"] == "s1" and c["exit_tag"] == "flip"

    d = _rt(rt, "X", 3)  # остаток переворота стал long-лотом по 95 и закрыт по 105
    assert d["side"] == "long" and d["qty"] == pytest.approx(1.0)
    assert d["entry_price"] == pytest.approx(95.0) and d["exit_price"] == pytest.approx(105.0)
    assert d["gross_pnl"] == pytest.approx(10.0)
    assert d["fees"] == pytest.approx(0.30 / 3 + 0.10)
    assert d["net_pnl"] == pytest.approx(9.8)
    assert d["open_ts"] == 5000 and d["close_ts"] == 8000 and d["hold_secs"] == pytest.approx(3.0)
    assert d["entry_tag"] == "flip" and d["exit_tag"] == "x2"


def test_short_roundtrip_other_symbol(fills):
    rt = roundtrips(fills)
    y = _rt(rt, "Y", 0)
    assert y["side"] == "short" and y["gross_pnl"] == pytest.approx(-2.0)
    assert y["net_pnl"] == pytest.approx(-2.02)
    assert y["entry_tag"] == "ys" and y["exit_tag"] == "yx"


def test_partial_entries_single_exit(fills):
    rt = roundtrips(fills)
    z = _rt(rt, "Z", 0)
    assert z["qty"] == pytest.approx(1.0) and z["n_fills"] == 3
    assert z["entry_price"] == pytest.approx(101.0)  # VWAP двух входов
    assert z["gross_pnl"] == pytest.approx(3.0) and z["fees"] == pytest.approx(0.2)
    assert z["net_pnl"] == pytest.approx(2.8)
    assert z["open_ts"] == 1000 and z["entry_tag"] == "a" and z["exit_tag"] == "c"
    assert z["maker_share"] == pytest.approx((0.5 + 1.0) / 2.0)  # входы: 0.5 maker + 0.5 taker; выход maker 1.0


def test_open_position_marked(fills):
    closed = roundtrips(fills)
    with_open = roundtrips(fills, include_open=True)
    assert len(with_open) == len(closed) + 1
    o = with_open[with_open["open"]].iloc[0]
    assert o["symbol"] == "X" and o["side"] == "long" and o["qty"] == pytest.approx(0.5)
    assert o["entry_price"] == pytest.approx(100.0) and o["entry_tag"] == "open"
    assert pd.isna(o["exit_price"]) and pd.isna(o["net_pnl"]) and pd.isna(o["close_ts"])


def test_totals_match_naive_pnl(fills):
    """Сумма брутто по раунд-трипам = PnL по всем закрытым позициям, сумма комиссий = комиссии закрытых частей."""
    rt = roundtrips(fills)
    assert rt["gross_pnl"].sum() == pytest.approx(4.0 - 6.0 + 10.0 + 10.0 - 2.0 + 3.0)
    total_fee = fills["fee"].sum()
    open_fee = 0.05  # комиссия открытого лота
    assert rt["fees"].sum() == pytest.approx(total_fee - open_fee)


def test_roundtrips_order_by_ts_when_seq_missing(fills):
    df = fills.drop(columns=["seq"]).sample(frac=1.0, random_state=1)
    rt = roundtrips(df)
    assert len(rt) == 6 and rt["gross_pnl"].sum() == pytest.approx(19.0)


def test_roundtrips_empty():
    rt = roundtrips(pd.DataFrame(columns=["symbol", "side", "price", "qty", "fee", "ts"]))
    assert len(rt) == 0 and list(rt.columns) == ROUNDTRIP_COLUMNS
    assert len(roundtrips(None)) == 0


# ---------------------------------------------------------------- сводка
def _run(**kw):
    base = {"run_id": 7, "name": "t", "mode": "backtest", "status": "finished", "strategy": "s", "initial_equity": 1000.0,
            "started_ts": 0, "finished_ts": 86_400_000}
    base.update(kw)
    return base


def _equity(points):
    return pd.DataFrame([{"ts": ts, "equity": eq, "realized_pnl": 0.0, "unrealized_pnl": 0.0, "fees": 0.0, "funding": 0.5,
                          "gross_notional": 0.0, "n_positions": 0, "n_fills": 0, "n_open_orders": 0, "max_drawdown": 0.0}
                         for ts, eq in points])


def test_summary_values(fills):
    rt = roundtrips(fills, include_open=True)
    eq = _equity([(0, 1000.0), (300_000, 1010.0), (600_000, 990.0), (900_000, 1020.0)])
    mk = pd.DataFrame([{"seq": 1, "symbol": "X", "horizon_ms": 5000, "mid": 1, "markout_bps": 2.0},
                       {"seq": 2, "symbol": "X", "horizon_ms": 5000, "mid": 1, "markout_bps": -1.0},
                       {"seq": 1, "symbol": "X", "horizon_ms": 1000, "mid": 1, "markout_bps": 0.5}])
    s = summary(_run(), eq, fills, rt, mk)
    assert s["net_pnl"] == pytest.approx(20.0) and s["return_pct"] == pytest.approx(2.0)
    assert s["fees"] == pytest.approx(fills["fee"].sum())
    assert s["funding"] == pytest.approx(0.5)
    assert s["n_fills"] == 12 and s["n_roundtrips"] == 6 and s["n_open_lots"] == 1
    assert s["trades_per_day"] == pytest.approx(6.0)
    assert s["n_wins"] == 4 and s["n_losses"] == 2 and s["win_rate"] == pytest.approx(4 / 6)
    wins = 3.91 + 9.6 + 9.8 + 2.8
    losses = 6.12 + 2.02
    assert s["profit_factor"] == pytest.approx(wins / losses)
    assert s["expectancy_per_trade"] == pytest.approx((wins - losses) / 6)
    assert s["best_trade"] == pytest.approx(9.8) and s["worst_trade"] == pytest.approx(-6.12)
    assert s["max_drawdown"] == pytest.approx(20.0) and s["max_drawdown_pct"] == pytest.approx(20 / 1010 * 100)
    assert s["markout_bps"]["5000"] == pytest.approx(0.5) and s["markout_bps"]["1000"] == pytest.approx(0.5)
    assert s["markout_bps"]["30000"] is None
    assert s["pnl_long"] == pytest.approx(3.91 - 6.12 + 9.8 + 2.8) and s["pnl_short"] == pytest.approx(9.6 - 2.02)
    assert s["pnl_by_symbol"]["Y"] == pytest.approx(-2.02)
    assert s["pnl_by_tag"]["flip"] == pytest.approx(9.8)
    assert sum(h["n"] for h in s["pnl_by_hour"]) == 6 and len(s["pnl_by_hour"]) == 24
    assert s["maker_share"] == pytest.approx(9 / 12)
    v = s["verdict"]
    assert v["checks"]["profitable_after_fees"]["ok"] is True
    assert v["checks"]["enough_trades"]["ok"] is False
    assert v["checks"]["profit_factor"]["ok"] is True
    assert v["checks"]["markout_5s"]["ok"] is True
    assert v["checks"]["drawdown_pct"]["ok"] is True  # 1.98% < 10%
    assert v["n_total"] == 7
    # JSON-совместимость: нигде нет NaN и numpy-типов
    import json
    json.dumps(s, allow_nan=False)


def test_summary_empty_never_raises():
    empty = pd.DataFrame()
    s = summary(_run(finished_ts=None), empty, empty, roundtrips(empty), empty, now_ms=3_600_000)
    assert s["net_pnl"] == 0.0 and s["n_fills"] == 0 and s["n_roundtrips"] == 0
    assert s["win_rate"] is None and s["profit_factor"] is None and s["sharpe"] is None
    assert s["max_drawdown"] == 0.0 and s["duration_secs"] == pytest.approx(3600.0)
    checks = s["verdict"]["checks"]
    assert not checks["profitable_after_fees"]["ok"] and not checks["enough_trades"]["ok"] and not checks["profit_factor"]["ok"]
    assert checks["drawdown_pct"]["ok"]  # нулевая просадка формально проходит порог
    assert s["verdict"]["n_ok"] == 1
    s2 = summary({}, None, None, None, None)
    assert s2["net_pnl"] == 0.0 and s2["initial_equity"] == 0.0 and s2["return_pct"] is None
    assert all(h["n"] == 0 for h in s2["pnl_by_hour"])


def test_summary_without_equity_uses_fills(fills):
    s = summary(_run(), None, fills, roundtrips(fills), None)
    assert s["gross_pnl"] == pytest.approx(0.0)  # realized_pnl в синтетике нулевой
    assert s["net_pnl"] == pytest.approx(-fills["fee"].sum())


# ---------------------------------------------------------------- equity
def test_equity_curve_drawdown_and_resample():
    eq = _equity([(0, 100.0), (60_000, 110.0), (120_000, 99.0), (180_000, 120.0), (600_000, 108.0)])
    c = equity_curve(eq)
    assert list(c.columns) == ["ts", "equity", "peak", "drawdown", "drawdown_pct"]
    assert c["peak"].tolist() == [100.0, 110.0, 110.0, 120.0, 120.0]
    assert c["drawdown"].tolist() == pytest.approx([0.0, 0.0, 11.0, 0.0, 12.0])
    assert c["drawdown_pct"].iloc[2] == pytest.approx(10.0)
    r = equity_curve(eq, rule="5min")
    assert r["ts"].tolist() == [0, 300_000, 600_000]  # мс, а не наносекунды
    assert r["equity"].tolist() == [120.0, 120.0, 108.0]  # last() и ffill
    assert len(equity_curve(pd.DataFrame())) == 0


def test_sharpe_computed_on_long_series():
    pts = [(i * 300_000, 1000.0 + (i % 7) * 0.5 + i * 0.01) for i in range(300)]
    s = summary(_run(finished_ts=300 * 300_000), _equity(pts), None, None, None)
    assert s["sharpe"] is not None and math.isfinite(s["sharpe"])
    assert s["sortino"] is not None and s["n_return_periods"] == 299


def test_downsample_keeps_ends():
    df = pd.DataFrame({"x": range(1000)})
    d = downsample(df, 100)
    assert len(d) <= 101 and d["x"].iloc[0] == 0 and d["x"].iloc[-1] == 999
    assert len(downsample(df, 5000)) == 1000


def test_to_jsonable():
    import numpy as np
    out = to_jsonable({"a": np.float64("nan"), "b": np.int64(3), "c": [np.float32(1.5), None], "d": pd.NaT, "e": np.bool_(True)})
    assert out == {"a": None, "b": 3, "c": [1.5, None], "d": None, "e": True}
