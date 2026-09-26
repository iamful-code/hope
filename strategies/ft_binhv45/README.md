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

### Бэктест-фильтр (архив сделок → свечи 1m, 2026-09-17 … 25, `BinHV45LS`, stake 100 USDT)

| Набор | Свечей | Входов (лонг / шорт) | Чистый PnL |
|---|---|---|---|
| BTC, ETH, SOL, XRP, DOGE, ADA | 77 760 | **0 / 0** | 0 |
| 9 волатильных альтов (1000PEPE, SUI, WIF, WLD, ASTER, PUMPFUN, 1000BONK, MOODENG, TRUMP) | 116 640 | **0 / 0** | 0 |

Почему: офлайн-подсчёт условий на 194 400 минутных свечах 15 символов — движение за одну свечу > 1.7 %
(`closedelta > close·17/1000`) встретилось всего **25 раз** (BTC/ETH/SOL/XRP/ADA — ни разу), широкая полоса
(`bbdelta > 0.7 %`) — в 30 % свечей, а полный набор условий (плюс прокол нижней/верхней полосы и короткий хвост) —
**0 раз**. Пороги стратегии калиброваны под альты Binance 2018 года и на перпетуалах Bybit 2026 года не срабатывают.

### Статус

`✗ не прошла фильтр` — ноль сделок за 9 дней на 15 символах. Логика (реверсия после резкого прокола полосы)
жизнеспособна, но пороги надо переоптимизировать под Bybit: `freqtrade hyperopt --spaces buy` на данных Bybit
(`buy_bbdelta`, `buy_closedelta`, `buy_tail`) либо ручное ослабление `closedelta` до 0.4–0.6 % с проверкой на
бэктесте `--mode candles`; при ROI 1.25 % и тейкерном круге 0.11 % запас остаётся. Это следующий шаг, если стратегия
будет возвращена в очередь.
