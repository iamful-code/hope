"""Адаптер Freqtrade в движке hope: синтетический ряд свечей должен дать входы/выходы через should_exit.
Пропускается, если freqtrade не установлен (extra [freqtrade])."""

import math
from pathlib import Path

import pytest

pytest.importorskip("freqtrade")

from hope.config import load_config  # noqa: E402
from hope.engine.core import EngineCore  # noqa: E402
from hope.store.db import Store  # noqa: E402
from hope.engine.strategy import load_strategy_class  # noqa: E402
from hope.types import Bbo, BboEvent, Candle, KlineEvent, SymbolMeta  # noqa: E402

STRATEGY = '''
from freqtrade.strategy import IStrategy
from pandas import DataFrame
from technical import qtpylib

class SyntheticBB(IStrategy):
    INTERFACE_VERSION = 3
    minimal_roi = {"0": 0.01}
    stoploss = -0.02
    timeframe = "1m"
    startup_candle_count = 25
    can_short = True
    use_exit_signal = True

    def populate_indicators(self, df: DataFrame, metadata: dict) -> DataFrame:
        bb = qtpylib.bollinger_bands(df["close"], window=20, stds=1.5)
        df["lower"], df["upper"], df["mid"] = bb["lower"], bb["upper"], bb["mid"]
        return df

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        df.loc[df["close"] < df["lower"], ["enter_long", "enter_tag"]] = (1, "low")
        df.loc[df["close"] > df["upper"], ["enter_short", "enter_tag"]] = (1, "high")
        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        df.loc[df["close"] > df["mid"], "exit_long"] = 1
        df.loc[df["close"] < df["mid"], "exit_short"] = 1
        return df

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        # как в community-стратегиях: читаем проанализированный DataFrame через self.dp
        df, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        if df is None or len(df) == 0:
            return "dp_empty"
        if current_profit > 0.004:
            return "custom_tp"
        return None
'''


@pytest.fixture
def strategy_file(tmp_path: Path) -> Path:
    p = tmp_path / "SyntheticBB.py"
    p.write_text(STRATEGY)
    return p


def test_freqtrade_adapter_trades_synthetic_series(strategy_file: Path, tmp_path: Path):
    cfg = load_config(
        overrides={
            "run": {"data_dir": str(tmp_path)},
            "exchange": {"symbols": ["XUSDT"]},
            "strategy": {
                "class": "hope.adapters.freqtrade.runner:FreqtradeStrategy",
                "timeframe": "1m",
                "history_bars": 200,
                "params": {"strategy_path": str(strategy_file), "class_name": "SyntheticBB", "stake_amount_usd": 100.0,
                           "max_open_trades": 2, "check_secs": 1},
            },
        }
    )
    strategy = load_strategy_class(cfg.strategy.class_path)(cfg.strategy.params)
    meta = SymbolMeta(symbol="XUSDT", tick_size=0.01, qty_step=0.001, min_qty=0.001, price_scale=2, source="rest")
    store = Store(tmp_path / "t.db")
    store.start_run("t", "syn", "backtest", 10000.0, ["XUSDT"], {})
    core = EngineCore(cfg, strategy, store, "backtest", {"XUSDT": meta}, ["XUSDT"], use_book=False)
    core.candles_only = True
    t0 = 1_700_000_000_000
    core.start(t0)
    # синус с периодом 40 свечей: цена регулярно выходит за полосы Боллинджера и возвращается к середине
    for i in range(400):
        ts_open = t0 + i * 60_000
        px = 100.0 + 3.0 * math.sin(i / 40 * 2 * math.pi)
        c = Candle(ts_open, ts_open + 60_000, px, px + 0.1, px - 0.1, px, 1.0, px, 1, 0.5, closed=True)
        ts = c.ts_close
        core.tick(ts - 1)
        core.handle(BboEvent("XUSDT", Bbo(ts, px - 0.005, px + 0.005, 1e9, 1e9)), ts)
        core.handle(KlineEvent("XUSDT", "1", c), ts)
        core.tick(ts + cfg.paper.latency_ms + 1)
    core.stop(core.now)
    pf = core.pf
    assert pf.n_fills >= 6, "ожидались входы и выходы по синтетическому ряду"
    store.finish_run("finished", core.summary())
    store.close()
    import sqlite3

    con = sqlite3.connect(tmp_path / "t.db")
    rows = con.execute("select purpose, side, tag from fills").fetchall()
    entry_tags = {t for pur, _, t in rows if pur == "entry"}
    exit_tags = {t for pur, _, t in rows if pur == "exit"}
    sides = {s for pur, s, _ in rows if pur == "entry"}
    # были и лонги, и шорты (теги входа), и хотя бы один выход по сигналу/ROI
    assert "low" in entry_tags and "high" in entry_tags and sides == {"Buy", "Sell"}
    assert exit_tags & {"exit_signal", "roi", "stop_loss", "trailing_stop_loss", "custom_tp"}
    assert "dp_empty" not in exit_tags, "custom_exit не видит DataFrame через self.dp"
    assert "custom_tp" in exit_tags, "custom_exit с self.dp должен срабатывать"
    assert len(rows) == pf.n_fills
