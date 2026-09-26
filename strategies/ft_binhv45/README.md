# BinHV45 — Freqtrade, 1m, реверсия после прокола полосы Боллинджера

**Ветка:** `strategy/ft-binhv45` · **Движок:** Freqtrade (dry-run на Bybit через `adapters/freqtrade/`) и адаптер
`hope.adapters.freqtrade.runner` (бэктест-фильтр и paper без REST) · **Рынок:** Bybit linear USDT-перпетуалы ·
**Тип:** событийная реверсия на 1m, тейкерные входы/выходы, лонг + зеркальный шорт (`BinHV45LS`).

## Источник

[freqtrade/freqtrade-strategies](https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/berlinguyinca/BinHV45.py)
(GPL-3.0; файл `BinHV45.py` сохранён с лицензионной пометкой). Изменения: импорт `qtpylib` из пакета `technical`,
`startup_candle_count = 50`, добавлен класс `BinHV45LS` с зеркальными условиями для шорта.

## Логика

Индикаторы: Боллинджер (40, 2σ) по close; `bbdelta = mid − lower`, `closedelta = |close − close[-1]|`, `tail = close − low`.
Вход в лонг, если одновременно: `bbdelta > close·7/1000`, `closedelta > close·17/1000`, `tail < bbdelta·25/1000`,
`close < lower[-1]`, `close ≤ close[-1]`. Зеркальный шорт (`BinHV45LS`): `close > upper[-1]`, рост за свечу > 1.7 %,
короткий верхний хвост. Выход: ROI 1.25 % или стоп −5 %; сигналов выхода нет.

Порог `closedelta > 1.7 %` за одну минуту — редкое событие на ликвидных перпетуалах 2026 года (параметры подбирались
под альты Binance 2018), поэтому ожидается мало сделок; при провале фильтра следующий шаг — hyperopt порогов
на данных Bybit (`buy_bbdelta`, `buy_closedelta`, `buy_tail`).

## Запуск

```bash
uv pip install -p .venv/bin/python -e ".[freqtrade]"
hope run -c strategies/ft_binhv45/config.yaml                # class_name: BinHV45LS (лонг+шорт) или BinHV45
hope backtest -c strategies/ft_binhv45/config.yaml --mode candles --from 2026-09-17 --to 2026-09-25 --db data/bt_ft_binhv45.db
# настоящий Freqtrade: cp strategies/ft_binhv45/BinHV45.py adapters/freqtrade/user_data/strategies/ ; FT_STRATEGY=BinHV45LS
```

## Результаты

### Бэктест-фильтр (архив сделок → свечи 1m, 6 символов, 2026-09-17 … 25)

_бэктест выполняется; результаты будут добавлены следующим коммитом_

### Статус

`⏳ бэктест-фильтр`
