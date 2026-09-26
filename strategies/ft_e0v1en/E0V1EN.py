"""E0V1EN — переписанная (clean-room) версия популярной community-стратегии Freqtrade.

Источник правил: https://github.com/ssssi/freqtrade_strs (binance/dry_run/E0V1EN.py, лицензия в репозитории
не указана, поэтому код написан заново по описанию правил; имена индикаторов и тегов сохранены для
сопоставимости). CTI считается собственной функцией вместо pandas_ta (нет колёс под Python 3.11).

Идея: покупка «просадки» на 5m — RSI(20) падает, быстрый RSI(4) низкий, цена ниже SMA(15) на 2.7–4 %,
CTI(20) < 0.69, без экстремального движения за 24 ч. Выход: trailing-стоп после +3 %, custom_exit по
Stoch fastk / CCI / времени в позиции. Только лонг.
"""

from __future__ import annotations

from datetime import timedelta
from functools import reduce

import numpy as np
import talib.abstract as ta
from freqtrade.persistence import Trade
from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy
from pandas import DataFrame, Series


def cti(close: Series, length: int = 20) -> Series:
    """Correlation Trend Indicator: коэффициент корреляции цены с временем в окне (как pandas_ta.cti)."""
    x = np.arange(length, dtype=float)
    x = x - x.mean()
    sx = np.sqrt((x**2).sum())

    def _r(w: np.ndarray) -> float:
        y = w - w.mean()
        sy = np.sqrt((y**2).sum())
        return float((x * y).sum() / (sx * sy)) if sy > 0 else 0.0

    return close.rolling(length).apply(_r, raw=True)


class E0V1EN(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "5m"
    minimal_roi = {"0": 1}
    stoploss = -0.25
    trailing_stop = True
    trailing_stop_positive = 0.002
    trailing_stop_positive_offset = 0.03
    trailing_only_offset_is_reached = True
    process_only_new_candles = True
    use_exit_signal = True
    can_short = False
    # 24h_change_pct использует сдвиг на 288 свечей — нужен запас истории
    startup_candle_count = 300

    order_types = {
        "entry": "market",
        "exit": "market",
        "emergency_exit": "market",
        "force_entry": "market",
        "force_exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    buy_rsi_fast_32 = IntParameter(20, 70, default=40, space="buy", optimize=False)
    buy_rsi_32 = IntParameter(15, 50, default=42, space="buy", optimize=False)
    buy_sma15_32 = DecimalParameter(0.900, 1, default=0.973, decimals=3, space="buy", optimize=False)
    buy_cti_32 = DecimalParameter(-1, 1, default=0.69, decimals=2, space="buy", optimize=False)
    buy_24h_min_pct = DecimalParameter(-30.0, 0.0, default=-15.0, decimals=1, space="buy", optimize=False)
    buy_24h_max_pct = DecimalParameter(0.0, 200.0, default=50.0, decimals=1, space="buy", optimize=False)
    sell_fastx = IntParameter(50, 100, default=84, space="sell", optimize=False)

    @property
    def protections(self):
        return [{"method": "CooldownPeriod", "stop_duration_candles": 96}]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma_15"] = ta.SMA(dataframe, timeperiod=15)
        dataframe["cti"] = cti(dataframe["close"], 20)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["rsi_fast"] = ta.RSI(dataframe, timeperiod=4)
        dataframe["rsi_slow"] = ta.RSI(dataframe, timeperiod=20)
        dataframe["24h_change_pct"] = dataframe["close"].pct_change(periods=288) * 100
        stoch_fast = ta.STOCHF(dataframe, 5, 3, 0, 3, 0)
        dataframe["fastk"] = stoch_fast["fastk"]
        dataframe["cci"] = ta.CCI(dataframe, timeperiod=20)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "enter_tag"] = ""
        range_ok = (dataframe["24h_change_pct"] > self.buy_24h_min_pct.value) & (
            dataframe["24h_change_pct"] < self.buy_24h_max_pct.value
        )
        rsi_slow_falling = dataframe["rsi_slow"] < dataframe["rsi_slow"].shift(1)
        buy_1 = (
            rsi_slow_falling
            & (dataframe["rsi_fast"] < self.buy_rsi_fast_32.value)
            & (dataframe["rsi"] > self.buy_rsi_32.value)
            & (dataframe["close"] < dataframe["sma_15"] * self.buy_sma15_32.value)
            & (dataframe["cti"] < self.buy_cti_32.value)
            & range_ok
        )
        buy_new = (
            rsi_slow_falling
            & (dataframe["rsi_fast"] < 34)
            & (dataframe["rsi"] > 28)
            & (dataframe["close"] < dataframe["sma_15"] * 0.96)
            & (dataframe["cti"] < self.buy_cti_32.value)
            & range_ok
        )
        # как в оригинале: теги конкатенируются без разделителя (buy_1buy_new, если сработали оба)
        dataframe.loc[buy_1, "enter_tag"] += "buy_1"
        dataframe.loc[buy_new, "enter_tag"] += "buy_new"
        dataframe.loc[reduce(lambda a, b: a | b, [buy_1, buy_new]), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, ["exit_long", "exit_tag"]] = (0, "long_out")
        return dataframe

    def custom_exit(self, pair: str, trade: Trade, current_time, current_rate: float, current_profit: float, **kwargs):
        dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        if dataframe is None or len(dataframe) == 0:
            return None
        candle = dataframe.iloc[-1].squeeze()
        if current_profit > 0 and str(trade.enter_tag) == "buy_new" and candle["fastk"] > self.sell_fastx.value:
            return "fastk_profit_sell"
        if current_profit > -0.03 and candle["cci"] > 80:
            return "cci_loss_sell"
        if current_time - timedelta(hours=7) > trade.open_date_utc and current_profit >= -0.05:
            return "time_loss_sell_7_5"
        if current_time - timedelta(hours=10) > trade.open_date_utc and current_profit >= -0.1:
            return "time_loss_sell_10_10"
        return None
