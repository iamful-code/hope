"""Метрики стратегии по данным БД результатов.

Все функции чистые: принимают DataFrame'ы (fills, equity, markouts, ...) и возвращают
DataFrame/dict. Устойчивы к пустым данным: при отсутствии сделок возвращают нули/None,
никогда не бросают исключений из-за пустого входа.

Раунд-трип (round trip) — закрытая часть позиции. Строится FIFO-сопоставлением по символу:
каждое исполнение, сокращающее позицию, «съедает» самые старые открытые лоты и порождает
одну строку раунд-трипа (qty = закрытое количество, entry_price = VWAP съеденных лотов).
Исполнение-переворот делится на две части: закрывающую (попадает в раунд-трип) и
открывающую (становится новым лотом противоположного направления). Частичные исполнения
обрабатываются естественно: лот может закрываться несколькими выходами, один выход может
закрывать несколько лотов. Сопоставление ведётся по знаку позиции, а не по полю purpose —
поле purpose/tag используется только как аннотация (entry_tag / exit_tag).
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any

import numpy as np
import pandas as pd

ROUNDTRIP_COLUMNS = [
    "symbol",
    "side",  # long | short
    "open_ts",
    "close_ts",
    "qty",
    "entry_price",
    "exit_price",
    "gross_pnl",
    "fees",
    "net_pnl",
    "hold_secs",
    "n_fills",
    "entry_tag",
    "exit_tag",
    "maker_share",
    "entry_seq",
    "exit_seq",
    "open",  # True — позиция ещё не закрыта (только при include_open=True)
]

MARKOUT_HORIZONS_MS = (1000, 5000, 30000, 60000)
_EPS = 1e-12

# Пороги «вердикта» — эвристики для быстрого чтения результата
VERDICT_MIN_TRADES = 100
VERDICT_MIN_PROFIT_FACTOR = 1.2
VERDICT_MAX_FEE_SHARE = 0.5
VERDICT_MAX_DD_PCT = 10.0
VERDICT_MIN_SHARPE = 1.0


# ====================================================================== утилиты
def _f(x: Any, default: float | None = None) -> float | None:
    """Аккуратно привести к float; NaN/None -> default."""
    if x is None:
        return default
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


def _get(row: Any, key: str, default: Any = None) -> Any:
    """Достать поле из dict / sqlite3.Row / pd.Series / объекта с атрибутами."""
    if row is None:
        return default
    try:
        if isinstance(row, dict):
            return row.get(key, default)
        if hasattr(row, "keys") and key in row.keys():  # sqlite3.Row, pd.Series
            return row[key]
    except Exception:  # noqa: BLE001 — любая экзотика -> default
        pass
    return getattr(row, key, default)


def to_jsonable(obj: Any) -> Any:
    """Рекурсивно превратить numpy/pandas-типы в родные python; NaN/NaT -> None."""
    if obj is None:
        return None
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (pd.Timestamp,)):
        return None if pd.isna(obj) else obj.isoformat()
    if obj is pd.NaT or obj is pd.NA:
        return None
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, np.ndarray, pd.Series, pd.Index)):
        return [to_jsonable(v) for v in list(obj)]
    if isinstance(obj, pd.DataFrame):
        return [to_jsonable(r) for r in obj.to_dict(orient="records")]
    if hasattr(obj, "item"):
        try:
            return to_jsonable(obj.item())
        except Exception:  # noqa: BLE001
            pass
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    return obj


def _col(df: pd.DataFrame, name: str, default: Any) -> pd.Series:
    """Столбец DataFrame или серия-заполнитель той же длины."""
    if name in df.columns:
        return df[name]
    return pd.Series([default] * len(df), index=df.index)


def _side_sign(side: pd.Series) -> pd.Series:
    s = side.astype(str).str.lower()
    return pd.Series(np.where(s.isin(["buy", "b", "long", "bid"]), 1.0, -1.0), index=side.index)


def _ordered_fills(fills_df: pd.DataFrame) -> pd.DataFrame:
    """Исполнения в порядке обработки движком: по seq, если он уникален, иначе по ts."""
    if fills_df is None or len(fills_df) == 0:
        return pd.DataFrame(columns=["symbol", "side", "price", "qty", "fee", "ts", "is_maker", "tag", "seq"])
    df = fills_df.copy()
    if "seq" in df.columns and df["seq"].nunique() == len(df) and (df["seq"] > 0).all():
        df = df.sort_values(["seq"], kind="mergesort")
    else:
        keys = ["ts"] + (["fill_id"] if "fill_id" in df.columns else [])
        df = df.sort_values(keys, kind="mergesort")
        if "seq" not in df.columns or df["seq"].nunique() != len(df):
            df["seq"] = np.arange(1, len(df) + 1)
    return df


def spread_captured_bps(fills_df: pd.DataFrame) -> pd.Series:
    """Захваченный спред каждого исполнения, б.п.: sign*(mid - price)/price*1e4 по bid/ask на момент
    исполнения (покупка ниже mid — положительно). NaN, если котировок нет."""
    if fills_df is None or len(fills_df) == 0:
        return pd.Series(dtype=float)
    bid = pd.to_numeric(_col(fills_df, "bid", 0.0), errors="coerce").fillna(0.0)
    ask = pd.to_numeric(_col(fills_df, "ask", 0.0), errors="coerce").fillna(0.0)
    price = pd.to_numeric(fills_df["price"], errors="coerce")
    ok = (bid > 0) & (ask > 0) & (price > 0)
    mid = (bid + ask) * 0.5
    sign = _side_sign(fills_df["side"])
    out = sign * (mid - price) / price * 1e4
    return out.where(ok)


# ====================================================================== раунд-трипы
def roundtrips(fills_df: pd.DataFrame, include_open: bool = False) -> pd.DataFrame:
    """FIFO-сопоставление исполнений в раунд-трипы (см. докстринг модуля).

    Ожидаемые столбцы fills_df: symbol, side (Buy/Sell), price, qty, fee, ts (мс), is_maker, tag, seq.
    Возвращает DataFrame со столбцами ROUNDTRIP_COLUMNS, отсортированный по close_ts.
    При include_open=True в конец добавляются незакрытые лоты (open=True, exit_* = NaN).
    """
    df = _ordered_fills(fills_df)
    rows: list[dict] = []
    if len(df) == 0:
        return pd.DataFrame(columns=ROUNDTRIP_COLUMNS)

    sym = df["symbol"].astype(str).to_numpy()
    sign_arr = _side_sign(df["side"]).to_numpy()
    price_arr = pd.to_numeric(df["price"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    qty_arr = pd.to_numeric(df["qty"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    fee_arr = pd.to_numeric(_col(df, "fee", 0.0), errors="coerce").fillna(0.0).to_numpy(dtype=float)
    ts_arr = pd.to_numeric(df["ts"], errors="coerce").fillna(0).to_numpy(dtype=np.int64)
    maker_arr = pd.to_numeric(_col(df, "is_maker", 0), errors="coerce").fillna(0).to_numpy() != 0
    tag_arr = _col(df, "tag", "").fillna("").astype(str).to_numpy()
    seq_arr = pd.to_numeric(df["seq"], errors="coerce").fillna(0).to_numpy(dtype=np.int64)

    # состояние по символам: направление и очередь открытых лотов
    lots_by_sym: dict[str, deque[dict]] = {}
    dir_by_sym: dict[str, float] = {}

    for i in range(len(df)):
        q = qty_arr[i]
        if q <= _EPS:
            continue
        s = sym[i]
        sign = sign_arr[i]
        fee_unit = fee_arr[i] / q
        lots = lots_by_sym.setdefault(s, deque())
        direction = dir_by_sym.get(s, 0.0)
        remaining = q

        if direction == 0.0 or sign == direction or not lots:
            lots.append(
                {"qty": q, "price": price_arr[i], "ts": ts_arr[i], "fee_unit": fee_unit,
                 "tag": tag_arr[i], "maker": maker_arr[i], "seq": seq_arr[i]}
            )
            dir_by_sym[s] = sign
            continue

        # закрытие FIFO
        consumed: list[tuple[dict, float]] = []
        closing = 0.0
        while remaining > _EPS and lots:
            lot = lots[0]
            take = min(lot["qty"], remaining)
            lot["qty"] -= take
            remaining -= take
            closing += take
            consumed.append((lot, take))
            if lot["qty"] <= _EPS:
                lots.popleft()
        if closing > _EPS:
            entry_notional = sum(lot["price"] * t for lot, t in consumed)
            entry_fees = sum(lot["fee_unit"] * t for lot, t in consumed)
            exit_fee = fee_unit * closing
            gross = direction * (price_arr[i] * closing - entry_notional)
            maker_qty = sum(t for lot, t in consumed if lot["maker"]) + (closing if maker_arr[i] else 0.0)
            first = consumed[0][0]
            fees = entry_fees + exit_fee
            rows.append(
                {
                    "symbol": s,
                    "side": "long" if direction > 0 else "short",
                    "open_ts": int(first["ts"]),
                    "close_ts": int(ts_arr[i]),
                    "qty": closing,
                    "entry_price": entry_notional / closing,
                    "exit_price": price_arr[i],
                    "gross_pnl": gross,
                    "fees": fees,
                    "net_pnl": gross - fees,
                    "hold_secs": (int(ts_arr[i]) - int(first["ts"])) / 1000.0,
                    "n_fills": len(consumed) + 1,
                    "entry_tag": first["tag"],
                    "exit_tag": tag_arr[i],
                    "maker_share": maker_qty / (2.0 * closing),
                    "entry_seq": int(first["seq"]),
                    "exit_seq": int(seq_arr[i]),
                    "open": False,
                }
            )
        if remaining > _EPS:
            # переворот: остаток открывает позицию в противоположную сторону
            lots.append(
                {"qty": remaining, "price": price_arr[i], "ts": ts_arr[i], "fee_unit": fee_unit,
                 "tag": tag_arr[i], "maker": maker_arr[i], "seq": seq_arr[i]}
            )
            dir_by_sym[s] = sign
        elif not lots:
            dir_by_sym[s] = 0.0

    if include_open:
        for s, lots in lots_by_sym.items():
            direction = dir_by_sym.get(s, 0.0)
            for lot in lots:
                if lot["qty"] <= _EPS:
                    continue
                rows.append(
                    {
                        "symbol": s,
                        "side": "long" if direction > 0 else "short",
                        "open_ts": int(lot["ts"]),
                        "close_ts": None,
                        "qty": lot["qty"],
                        "entry_price": lot["price"],
                        "exit_price": np.nan,
                        "gross_pnl": np.nan,
                        "fees": lot["fee_unit"] * lot["qty"],
                        "net_pnl": np.nan,
                        "hold_secs": np.nan,
                        "n_fills": 1,
                        "entry_tag": lot["tag"],
                        "exit_tag": "",
                        "maker_share": 1.0 if lot["maker"] else 0.0,
                        "entry_seq": int(lot["seq"]),
                        "exit_seq": None,
                        "open": True,
                    }
                )

    out = pd.DataFrame(rows, columns=ROUNDTRIP_COLUMNS)
    if len(out):
        out = out.sort_values(["open", "close_ts", "exit_seq"], kind="mergesort", na_position="last")
        out = out.reset_index(drop=True)
    return out


# ====================================================================== кривая equity
def equity_curve(equity_df: pd.DataFrame, rule: str | None = None) -> pd.DataFrame:
    """Кривая equity с пиком и просадкой.

    Возвращает DataFrame со столбцами ts (мс), equity, peak, drawdown (абс.), drawdown_pct (% от пика).
    rule — правило ресемплинга pandas (например '5min'): берётся последнее значение в окне,
    пропуски заполняются вперёд. Без rule — исходные точки.
    """
    cols = ["ts", "equity", "peak", "drawdown", "drawdown_pct"]
    if equity_df is None or len(equity_df) == 0 or "equity" not in equity_df.columns:
        return pd.DataFrame(columns=cols)
    df = equity_df[["ts", "equity"]].copy()
    df["ts"] = pd.to_numeric(df["ts"], errors="coerce")
    df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
    df = df.dropna().sort_values("ts", kind="mergesort")
    if len(df) == 0:
        return pd.DataFrame(columns=cols)
    if rule:
        idx = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
        ser = pd.Series(df["equity"].to_numpy(), index=idx)
        ser = ser.resample(rule).last().ffill()
        # pandas 3 сохраняет единицу измерения индекса (ms/us/ns) — приводим явно к миллисекундам
        ts_ms = ser.index.as_unit("ms").asi8.astype("int64")
        df = pd.DataFrame({"ts": ts_ms, "equity": ser.to_numpy()})
    eq = df["equity"].to_numpy(dtype=float)
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    with np.errstate(divide="ignore", invalid="ignore"):
        dd_pct = np.where(peak > 0, dd / peak * 100.0, 0.0)
    out = pd.DataFrame(
        {"ts": df["ts"].astype("int64").to_numpy(), "equity": eq, "peak": peak, "drawdown": dd, "drawdown_pct": dd_pct}
    )
    return out.reset_index(drop=True)


def downsample(df: pd.DataFrame, max_points: int) -> pd.DataFrame:
    """Проредить строки до max_points равномерным шагом, всегда сохраняя первую и последнюю."""
    n = len(df)
    if max_points is None or max_points <= 0 or n <= max_points:
        return df
    stride = int(math.ceil(n / max(max_points - 1, 1)))
    idx = list(range(0, n, stride))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return df.iloc[idx]


def _risk_ratios(equity_df: pd.DataFrame, rule: str = "5min") -> tuple[float | None, float | None, int]:
    """Годовые Sharpe и Sortino по доходностям equity, ресемплированной с шагом rule."""
    curve = equity_curve(equity_df, rule=rule)
    if len(curve) < 3:
        return None, None, len(curve)
    eq = curve["equity"].to_numpy(dtype=float)
    prev = eq[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.where(prev > 0, eq[1:] / prev - 1.0, 0.0)
    n = len(rets)
    if n < 2:
        return None, None, n
    step_ms = pd.Timedelta(rule).total_seconds() * 1000.0
    periods_per_year = 365.25 * 86_400_000.0 / step_ms
    mu = float(rets.mean())
    sd = float(rets.std(ddof=1))
    sharpe = mu / sd * math.sqrt(periods_per_year) if sd > 0 else None
    downside = rets[rets < 0]
    dsd = float(np.sqrt(np.mean(np.square(downside)))) if len(downside) else 0.0
    sortino = mu / dsd * math.sqrt(periods_per_year) if dsd > 0 else None
    return _f(sharpe), _f(sortino), n


# ====================================================================== сводка
def _pnl_by(rt: pd.DataFrame, key: str) -> dict[str, float]:
    if len(rt) == 0 or key not in rt.columns:
        return {}
    g = rt.groupby(rt[key].fillna("").astype(str))["net_pnl"].sum()
    return {str(k): float(v) for k, v in g.items()}


def _pnl_by_hour(rt: pd.DataFrame) -> list[dict]:
    out = [{"hour": h, "net_pnl": 0.0, "n": 0, "win_rate": None} for h in range(24)]
    if len(rt) == 0:
        return out
    hours = ((rt["close_ts"].astype("int64") // 3_600_000) % 24).astype(int)
    g = rt.groupby(hours)["net_pnl"]
    for h, ser in g:
        n = int(len(ser))
        out[int(h)] = {
            "hour": int(h),
            "net_pnl": float(ser.sum()),
            "n": n,
            "win_rate": float((ser > 0).mean()) if n else None,
        }
    return out


def _verdict(m: dict) -> dict:
    """Набор проверок «читаемых глазами»: ok/значение/порог/подпись."""
    pf = m.get("profit_factor")
    pf_ok = (pf is not None and pf > VERDICT_MIN_PROFIT_FACTOR) or (pf is None and (m.get("n_wins") or 0) > 0 and (m.get("n_losses") or 0) == 0)
    fee_share = m.get("fee_share_of_gross")
    mk5 = (m.get("markout_bps") or {}).get("5000")
    dd = m.get("max_drawdown_pct")
    sharpe = m.get("sharpe")
    checks = {
        "profitable_after_fees": {
            "ok": (m.get("net_pnl") or 0.0) > 0,
            "value": m.get("net_pnl"),
            "threshold": "> 0",
            "label": "Прибыль после комиссий",
        },
        "enough_trades": {
            "ok": (m.get("n_roundtrips") or 0) >= VERDICT_MIN_TRADES,
            "value": m.get("n_roundtrips"),
            "threshold": f">= {VERDICT_MIN_TRADES}",
            "label": "Достаточно сделок для статистики",
        },
        "profit_factor": {
            "ok": bool(pf_ok),
            "value": pf,
            "threshold": f"> {VERDICT_MIN_PROFIT_FACTOR}",
            "label": "Profit factor",
        },
        "fee_share": {
            "ok": fee_share is not None and fee_share < VERDICT_MAX_FEE_SHARE,
            "value": fee_share,
            "threshold": f"< {VERDICT_MAX_FEE_SHARE}",
            "label": "Доля комиссий в грязной прибыли",
        },
        "markout_5s": {
            "ok": mk5 is not None and mk5 >= 0,
            "value": mk5,
            "threshold": ">= 0 б.п.",
            "label": "Markout 5с (нет adverse selection)",
        },
        "drawdown_pct": {
            "ok": dd is not None and dd < VERDICT_MAX_DD_PCT,
            "value": dd,
            "threshold": f"< {VERDICT_MAX_DD_PCT}%",
            "label": "Максимальная просадка",
        },
        "sharpe": {
            "ok": sharpe is not None and sharpe >= VERDICT_MIN_SHARPE,
            "value": sharpe,
            "threshold": f">= {VERDICT_MIN_SHARPE}",
            "label": "Sharpe (5-мин доходности, годовой)",
        },
    }
    n_ok = sum(1 for c in checks.values() if c["ok"])
    return {"checks": checks, "n_ok": n_ok, "n_total": len(checks), "all_ok": n_ok == len(checks)}


def summary(
    run_row: Any,
    equity_df: pd.DataFrame | None,
    fills_df: pd.DataFrame | None,
    roundtrips_df: pd.DataFrame | None,
    markouts_df: pd.DataFrame | None,
    now_ms: int | None = None,
) -> dict:
    """Сводные метрики запуска. Все поля JSON-совместимы (numpy -> python, NaN -> None).

    run_row — строка таблицы runs (dict / sqlite3.Row / pd.Series); нужны started_ts, finished_ts,
    initial_equity, mode, status. Длительность: до finished_ts, иначе до последнего снимка
    equity / исполнения (для бэктеста это событийное время), иначе до now_ms.
    """
    fills = fills_df if fills_df is not None else pd.DataFrame()
    eq = equity_df if equity_df is not None else pd.DataFrame()
    rt_all = roundtrips_df if roundtrips_df is not None else pd.DataFrame(columns=ROUNDTRIP_COLUMNS)
    mk = markouts_df if markouts_df is not None else pd.DataFrame()
    if "open" in rt_all.columns and len(rt_all):
        rt = rt_all[~rt_all["open"].astype(bool)]
        n_open_lots = int(rt_all["open"].astype(bool).sum())
    else:
        rt, n_open_lots = rt_all, 0

    initial = _f(_get(run_row, "initial_equity"), 0.0) or 0.0
    started_f = _f(_get(run_row, "started_ts"))  # 0 — допустимая метка (синтетика), None — не задана
    started = int(started_f) if started_f is not None else None
    finished = _f(_get(run_row, "finished_ts"))
    mode = str(_get(run_row, "mode", "") or "")
    status = str(_get(run_row, "status", "") or "")

    # ---- equity / pnl
    last_eq = None
    if len(eq) and "equity" in eq.columns:
        e = eq.sort_values("ts", kind="mergesort")
        last_eq = e.iloc[-1]
    last_equity_ts = int(last_eq["ts"]) if last_eq is not None else None
    last_fill_ts = int(pd.to_numeric(fills["ts"], errors="coerce").max()) if len(fills) and "ts" in fills.columns else None

    n_fills = int(len(fills))
    fees = float(pd.to_numeric(_col(fills, "fee", 0.0), errors="coerce").fillna(0.0).sum()) if n_fills else 0.0
    gross = float(pd.to_numeric(_col(fills, "realized_pnl", 0.0), errors="coerce").fillna(0.0).sum()) if n_fills else 0.0

    if last_eq is not None:
        equity_now = _f(last_eq["equity"], initial) or 0.0
        funding = _f(last_eq.get("funding"), 0.0) or 0.0
        realized = _f(last_eq.get("realized_pnl"), gross) or 0.0
        unrealized = _f(last_eq.get("unrealized_pnl"), 0.0) or 0.0
        dd_engine = _f(last_eq.get("max_drawdown"))
    else:
        equity_now = initial + gross - fees
        funding, realized, unrealized, dd_engine = 0.0, gross, 0.0, None
    net_pnl = equity_now - initial
    return_pct = net_pnl / initial * 100.0 if initial > 0 else None
    fee_share = fees / abs(gross) if abs(gross) > _EPS else None

    # ---- длительность
    if finished:
        end_ts = int(finished)
    else:
        cands = [t for t in (last_equity_ts, last_fill_ts) if t]
        end_ts = max(cands) if cands else int(now_ms or pd.Timestamp.now(tz="UTC").value // 1_000_000)
    duration_secs = max(0.0, (end_ts - started) / 1000.0) if started is not None else 0.0
    duration_days = duration_secs / 86_400.0

    # ---- раунд-трипы
    n_rt = int(len(rt))
    if n_rt:
        pnl = pd.to_numeric(rt["net_pnl"], errors="coerce").fillna(0.0)
        wins, losses = pnl[pnl > 0], pnl[pnl < 0]
        n_wins, n_losses = int(len(wins)), int(len(losses))
        win_rate = n_wins / n_rt
        avg_win = float(wins.mean()) if n_wins else None
        avg_loss = float(losses.mean()) if n_losses else None
        profit_factor = float(wins.sum() / abs(losses.sum())) if n_losses and abs(losses.sum()) > _EPS else None
        expectancy = float(pnl.mean())
        hold = pd.to_numeric(rt["hold_secs"], errors="coerce").dropna()
        avg_hold = float(hold.mean()) if len(hold) else None
        med_hold = float(hold.median()) if len(hold) else None
        best, worst = float(pnl.max()), float(pnl.min())
        notional = pd.to_numeric(rt["qty"], errors="coerce") * pd.to_numeric(rt["entry_price"], errors="coerce")
        avg_notional = _f(notional.mean())
        side = rt["side"].astype(str)
        pnl_long = float(pnl[side == "long"].sum())
        pnl_short = float(pnl[side == "short"].sum())
        n_long, n_short = int((side == "long").sum()), int((side == "short").sum())
        rt_gross = float(pd.to_numeric(rt["gross_pnl"], errors="coerce").fillna(0.0).sum())
        rt_fees = float(pd.to_numeric(rt["fees"], errors="coerce").fillna(0.0).sum())
    else:
        n_wins = n_losses = 0
        win_rate = avg_win = avg_loss = profit_factor = expectancy = None
        avg_hold = med_hold = best = worst = avg_notional = None
        pnl_long = pnl_short = 0.0
        n_long = n_short = 0
        rt_gross = rt_fees = 0.0
    trades_per_day = n_rt / duration_days if duration_days > 0 else None
    fills_per_day = n_fills / duration_days if duration_days > 0 else None

    # ---- исполнения: maker, спред
    if n_fills:
        maker = pd.to_numeric(_col(fills, "is_maker", 0), errors="coerce").fillna(0) != 0
        maker_share = float(maker.mean())
        captured = spread_captured_bps(fills)
        maker_captured = captured[maker].dropna()
        avg_captured = float(maker_captured.mean()) if len(maker_captured) else None
        spread_place = pd.to_numeric(_col(fills, "spread_bps_at_place", np.nan), errors="coerce").dropna()
        avg_spread_place = float(spread_place.mean()) if len(spread_place) else None
        fill_notional = pd.to_numeric(fills["price"], errors="coerce") * pd.to_numeric(fills["qty"], errors="coerce")
        turnover = float(fill_notional.fillna(0.0).sum())
    else:
        maker_share = avg_captured = avg_spread_place = None
        turnover = 0.0

    # ---- просадка и риск
    curve = equity_curve(eq)
    if len(curve):
        max_dd = float(curve["drawdown"].max())
        max_dd_pct = float(curve["drawdown_pct"].max())
        peak_equity = float(curve["peak"].iloc[-1])
    else:
        max_dd = max(0.0, -net_pnl)
        max_dd_pct = max_dd / initial * 100.0 if initial > 0 else None
        peak_equity = max(initial, equity_now)
    sharpe, sortino, n_periods = _risk_ratios(eq)

    # ---- markout
    markout: dict[str, float | None] = {str(h): None for h in MARKOUT_HORIZONS_MS}
    if len(mk) and "horizon_ms" in mk.columns and "markout_bps" in mk.columns:
        g = mk.groupby(pd.to_numeric(mk["horizon_ms"], errors="coerce").astype("int64"))["markout_bps"].mean()
        for h, v in g.items():
            markout[str(int(h))] = _f(v)

    out: dict[str, Any] = {
        "run_id": _get(run_row, "run_id"),
        "name": _get(run_row, "name"),
        "mode": mode,
        "status": status,
        "strategy": _get(run_row, "strategy"),
        "started_ts": started,
        "end_ts": end_ts,
        "duration_secs": duration_secs,
        "duration_days": duration_days,
        "initial_equity": initial,
        "equity": equity_now,
        "peak_equity": peak_equity,
        "net_pnl": net_pnl,
        "return_pct": return_pct,
        "gross_pnl": gross,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "fees": fees,
        "fee_share_of_gross": fee_share,
        "funding": funding,
        "turnover": turnover,
        "n_fills": n_fills,
        "n_roundtrips": n_rt,
        "n_open_lots": n_open_lots,
        "trades_per_day": trades_per_day,
        "fills_per_day": fills_per_day,
        "win_rate": win_rate,
        "n_wins": n_wins,
        "n_losses": n_losses,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "expectancy_per_trade": expectancy,
        "avg_hold_secs": avg_hold,
        "median_hold_secs": med_hold,
        "maker_share": maker_share,
        "avg_trade_notional": avg_notional,
        "roundtrip_gross_pnl": rt_gross,
        "roundtrip_fees": rt_fees,
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "max_drawdown_engine": dd_engine,
        "sharpe": sharpe,
        "sortino": sortino,
        "n_return_periods": n_periods,
        "best_trade": best,
        "worst_trade": worst,
        "markout_bps": markout,
        "markout_5s_bps": markout.get("5000"),
        "avg_spread_captured_bps": avg_captured,
        "avg_spread_at_place_bps": avg_spread_place,
        "pnl_by_symbol": _pnl_by(rt, "symbol"),
        "pnl_by_hour": _pnl_by_hour(rt),
        "pnl_by_tag": _pnl_by(rt, "entry_tag"),
        "pnl_by_exit_tag": _pnl_by(rt, "exit_tag"),
        "pnl_long": pnl_long,
        "pnl_short": pnl_short,
        "n_long": n_long,
        "n_short": n_short,
        "last_equity_ts": last_equity_ts,
        "last_fill_ts": last_fill_ts,
    }
    out["verdict"] = _verdict(out)
    return to_jsonable(out)
