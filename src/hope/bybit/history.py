"""История для бэктеста: публичный архив сделок Bybit (public.bybit.com/trading/<SYMBOL>/<SYMBOL>YYYY-MM-DD.csv.gz).

Архив доступен без ключей и без гео-ограничений REST. Файлы кэшируются в data/history/trades/<symbol>/
в формате Parquet (ts, price, qty, side). Из сделок строятся свечи любого таймфрейма и BBO-прокси.
Для спота есть public.bybit.com/spot/, формат тот же (категория задаёт подпапку).
"""

from __future__ import annotations

import asyncio
import gzip
import io
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from ..types import Side, Trade

log = logging.getLogger(__name__)

PUBLIC_BASE = "https://public.bybit.com"


def _subdir(category: str) -> str:
    return "spot" if category == "spot" else "trading"


def daterange(from_: str | date, to: str | date) -> list[date]:
    a = date.fromisoformat(from_) if isinstance(from_, str) else from_
    b = date.fromisoformat(to) if isinstance(to, str) else to
    if b < a:
        raise ValueError("дата 'to' раньше 'from'")
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


class TradeHistory:
    def __init__(self, data_dir: str | Path = "data", category: str = "linear", concurrency: int = 4) -> None:
        self.root = Path(data_dir) / "history" / "trades" / category
        self.category = category
        self.sem = asyncio.Semaphore(concurrency)

    def path(self, symbol: str, day: date) -> Path:
        return self.root / symbol / f"{day.isoformat()}.parquet"

    def pending(self, symbols: list[str], from_: str | date, to: str | date) -> list[tuple[str, date]]:
        """Дни, которых ещё нет в локальном кэше (их придётся скачать)."""
        return [(s, d) for s in symbols for d in daterange(from_, to) if not self.path(s, d).exists()]

    async def ensure(self, symbols: list[str], from_: str | date, to: str | date, client: httpx.AsyncClient | None = None,
                     progress=None) -> list[tuple[str, date]]:
        """Скачать недостающие дни. Возвращает список (symbol, day), которых нет в архиве.
        progress(done, total) вызывается после каждого файла."""
        own = client is None
        client = client or httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        missing: list[tuple[str, date]] = []
        try:
            todo = self.pending(symbols, from_, to)
            tasks = [self._fetch(client, s, d) for s, d in todo]
            for i, coro in enumerate(asyncio.as_completed(tasks), 1):
                res = await coro
                if res is not None:
                    missing.append(res)
                if progress is not None:
                    try:
                        progress(i, len(tasks))
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if own:
                await client.aclose()
        return missing

    async def _fetch(self, client: httpx.AsyncClient, symbol: str, day: date) -> tuple[str, date] | None:
        url = f"{PUBLIC_BASE}/{_subdir(self.category)}/{symbol}/{symbol}{day.isoformat()}.csv.gz"
        async with self.sem:
            for attempt in range(3):
                try:
                    r = await client.get(url)
                    break
                except httpx.HTTPError as e:
                    if attempt == 2:
                        log.warning("%s: %s", url, e)
                        return (symbol, day)
                    await asyncio.sleep(1.5 * (attempt + 1))
            if r.status_code == 404:
                log.info("нет данных: %s %s", symbol, day)
                return (symbol, day)
            if r.status_code != 200:
                log.warning("%s -> HTTP %d", url, r.status_code)
                return (symbol, day)
            df = await asyncio.to_thread(self._parse_csv, r.content)
            p = self.path(symbol, day)
            p.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(df.to_parquet, p, index=False)
            log.info("скачано %s %s: %d сделок", symbol, day, len(df))
            return None

    @staticmethod
    def _parse_csv(content: bytes) -> pd.DataFrame:
        with gzip.open(io.BytesIO(content), "rt", encoding="utf-8") as f:
            df = pd.read_csv(f, usecols=["timestamp", "side", "size", "price"])
        out = pd.DataFrame(
            {
                "ts": (df["timestamp"].astype("float64") * 1000).round().astype("int64"),
                "price": df["price"].astype("float64"),
                "qty": df["size"].astype("float64"),
                "side": (df["side"] == "Buy").astype("int8"),  # 1 = тейкер купил
            }
        )
        out.sort_values("ts", kind="stable", inplace=True)
        out.reset_index(drop=True, inplace=True)
        return out

    def load(self, symbol: str, from_: str | date, to: str | date) -> pd.DataFrame:
        frames = []
        for d in daterange(from_, to):
            p = self.path(symbol, d)
            if p.exists():
                frames.append(pd.read_parquet(p))
        if not frames:
            return pd.DataFrame({"ts": np.array([], dtype="int64"), "price": np.array([]), "qty": np.array([]), "side": np.array([], dtype="int8")})
        return pd.concat(frames, ignore_index=True)

    @staticmethod
    def iter_trades(df: pd.DataFrame):
        ts = df["ts"].to_numpy()
        px = df["price"].to_numpy()
        qty = df["qty"].to_numpy()
        side = df["side"].to_numpy()
        buy, sell = Side.BUY, Side.SELL
        for i in range(len(ts)):
            yield Trade(int(ts[i]), float(px[i]), float(qty[i]), buy if side[i] else sell)


def candles_from_trades(df: pd.DataFrame, tf_ms: int) -> pd.DataFrame:
    """Свечи из сделок (векторно). Колонки как в CandleSeries.COLS."""
    if df.empty:
        return pd.DataFrame(columns=["ts_open", "open", "high", "low", "close", "volume", "turnover", "n_trades", "buy_volume"])
    b = df["ts"] // tf_ms * tf_ms
    g = df.groupby(b, sort=True)
    turnover = (df["price"] * df["qty"]).groupby(b).sum()
    buyvol = (df["qty"] * df["side"]).groupby(b).sum()
    out = pd.DataFrame(
        {
            "open": g["price"].first(),
            "high": g["price"].max(),
            "low": g["price"].min(),
            "close": g["price"].last(),
            "volume": g["qty"].sum(),
            "turnover": turnover,
            "n_trades": g.size(),
            "buy_volume": buyvol,
        }
    )
    out.index.name = "ts_open"
    out = out.reset_index()
    # заполнить пропуски плоскими свечами
    full = pd.DataFrame({"ts_open": np.arange(out["ts_open"].iloc[0], out["ts_open"].iloc[-1] + tf_ms, tf_ms)})
    out = full.merge(out, on="ts_open", how="left")
    out["close"] = out["close"].ffill()
    for c in ("open", "high", "low"):
        out[c] = out[c].fillna(out["close"])
    for c in ("volume", "turnover", "n_trades", "buy_volume"):
        out[c] = out[c].fillna(0)
    out["n_trades"] = out["n_trades"].astype("int64")
    return out


def utc_day_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
