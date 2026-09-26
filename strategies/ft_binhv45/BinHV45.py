# BinHV45 — стратегия из freqtrade/freqtrade-strategies (user_data/strategies/berlinguyinca/BinHV45.py).
# Лицензия исходного репозитория: GPL-3.0 (https://github.com/freqtrade/freqtrade-strategies/blob/main/LICENSE).
# Этот файл распространяется на условиях GPL-3.0; изменения: импорт qtpylib из пакета `technical`,
# добавлен класс BinHV45LS с зеркальными условиями для шорта (can_short = True).
#
# Идея: событийная реверсия на 1m — свеча закрылась ниже нижней полосы Боллинджера (40, 2σ) при широкой
# полосе и большом падении за свечу, с коротким «хвостом» вниз. Выход: ROI 1.25 % или стоп −5 %.
from freqtrade.strategy import IntParameter, IStrategy
from pandas import DataFrame
from technical import qtpylib


class BinHV45(IStrategy):
    INTERFACE_VERSION: int = 3

    minimal_roi = {"0": 0.0125}
    stoploss = -0.05
    timeframe = "1m"
    startup_candle_count = 50
    can_short = False

    buy_bbdelta = IntParameter(low=1, high=15, default=30, space="buy", optimize=True)
    buy_closedelta = IntParameter(low=15, high=20, default=30, space="buy", optimize=True)
    buy_tail = IntParameter(low=20, high=30, default=30, space="buy", optimize=True)

    buy_params = {"buy_bbdelta": 7, "buy_closedelta": 17, "buy_tail": 25}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        bollinger = qtpylib.bollinger_bands(dataframe["close"], window=40, stds=2)
        dataframe["upper"] = bollinger["upper"]
        dataframe["mid"] = bollinger["mid"]
        dataframe["lower"] = bollinger["lower"]
        dataframe["bbdelta"] = (dataframe["mid"] - dataframe["lower"]).abs()
        dataframe["pricedelta"] = (dataframe["open"] - dataframe["close"]).abs()
        dataframe["closedelta"] = (dataframe["close"] - dataframe["close"].shift()).abs()
        dataframe["tail"] = (dataframe["close"] - dataframe["low"]).abs()
        dataframe["tail_top"] = (dataframe["high"] - dataframe["close"]).abs()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                dataframe["lower"].shift().gt(0)
                & dataframe["bbdelta"].gt(dataframe["close"] * self.buy_bbdelta.value / 1000)
                & dataframe["closedelta"].gt(dataframe["close"] * self.buy_closedelta.value / 1000)
                & dataframe["tail"].lt(dataframe["bbdelta"] * self.buy_tail.value / 1000)
                & dataframe["close"].lt(dataframe["lower"].shift())
                & dataframe["close"].le(dataframe["close"].shift())
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, "exit_long"] = 0
        return dataframe


class BinHV45LS(BinHV45):
    """BinHV45 + зеркальный шорт: закрытие выше верхней полосы при широкой полосе и резком росте за свечу."""

    can_short = True

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        dataframe["bbdelta_top"] = (dataframe["upper"] - dataframe["mid"]).abs()
        dataframe.loc[
            (
                dataframe["upper"].shift().gt(0)
                & dataframe["bbdelta_top"].gt(dataframe["close"] * self.buy_bbdelta.value / 1000)
                & dataframe["closedelta"].gt(dataframe["close"] * self.buy_closedelta.value / 1000)
                & dataframe["tail_top"].lt(dataframe["bbdelta_top"] * self.buy_tail.value / 1000)
                & dataframe["close"].gt(dataframe["upper"].shift())
                & dataframe["close"].ge(dataframe["close"].shift())
            ),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[:, ["exit_long", "exit_short"]] = 0
        return dataframe
