# RUNBOOK: как гонять стратегии в лаборатории hope

## 0. Требования

* Машина в регионе, где Bybit не блокирует API (из США и ряда стран CloudFront отдаёт 403; публичный WebSocket
  обычно доступен даже там — движок умеет работать без REST).
* Docker + docker compose (или Python 3.11 + `uv`).
* ~2 ГБ диска на сутки записи потока по 60 символам (`store.record_market: true`), иначе десятки МБ в день.

## 1. Запуск одной стратегии (Docker)

> Windows: см. [`docs/WINDOWS.md`](WINDOWS.md) — `windows\install.cmd`, затем `windows\start.cmd`.

```bash
git clone https://github.com/iamful-code/hope && cd hope
git checkout strategy/queue-mm            # или strategy/ft-e0v1en, strategy/ft-binhv45 ...
cp .env.example .env                       # STRATEGY_CONFIG уже указывает на стратегию ветки; HOPE_EXTRAS=freqtrade для ft-*
docker compose up -d --build
docker compose logs -f engine              # лог движка
# монитор: http://localhost:8000  (вкладка «Обзор» → «Вердикт»)
```

Остановка: `docker compose down` (открытые paper-позиции не закрываются, они остаются в БД; запуск
получит статус finished). Данные — в `./data/hope.db`.

Без Docker:

```bash
uv venv .venv && uv pip install -p .venv/bin/python -e ".[dev]"        # + ".[freqtrade]" для веток ft-*
.venv/bin/hope run --restart               # стратегия из STRATEGY_CONFIG в .env; или -c strategies/<имя>/config.yaml
.venv/bin/hope monitor --db data/hope.db --port 8000 --open
```

## 2. Несколько стратегий параллельно

Каждая ветка — отдельный каталог (`git worktree add ../hope-ft-e0v1en strategy/ft-e0v1en`), свой `.env`
(`MONITOR_PORT=8001`, …) и свой `docker compose -p <имя> up -d`. Один общий монитор на все БД:

```bash
hope monitor --db ../hope-queue-mm/data/hope.db,../hope-ft-e0v1en/data/hope.db --port 8000
```

Вкладка «Сравнение» накладывает кривые equity разных запусков.

## 3. Как читать результаты

Вкладка «Обзор»: чистый PnL после комиссий и фандинга, число исполнений и кругов, сделок/день, win rate,
profit factor, доля комиссий, доля мейкерских исполнений, Sharpe, markout. Панель «Вердикт» (пороги по умолчанию):

| Проверка | Порог | Что значит провал |
|---|---|---|
| Прибыль после комиссий | > 0 | стратегия не окупает комиссии |
| Кругов | ≥ 100 | статистики недостаточно, продолжайте тест |
| Profit factor | > 1.2 | выигрыши едва покрывают проигрыши |
| Доля комиссий | < 50 % грязной прибыли | edge съедают комиссии — увеличить размер сделки / целевой спред |
| Markout +5 с | ≥ 0 б.п. | после наших исполнений цена идёт против нас (adverse selection) |
| Просадка | < 10 % | риск неприемлем |
| Sharpe (5-мин) | ≥ 1 | результат неотличим от шума |

Стратегия «рабочая», если все проверки зелёные на живом paper-тесте длиной не меньше недели (для мейкерских —
и на реплее записанного потока с другими параметрами, чтобы убедиться, что результат не случайность).

## 4. Цикл перебора стратегии

1. Выбрать кандидата из `docs/strategy_catalog.md` (Top-15, порядок по движкам).
2. Создать ветку `strategy/<имя>` от базовой, положить код в `strategies/<имя>/` (`strategy.py` + `config.yaml`
   + `README.md`; для Freqtrade — файл стратегии без изменений и `class: hope.adapters.freqtrade.runner:FreqtradeStrategy`),
   указать её конфиг в `STRATEGY_CONFIG` в `.env.example` (и `HOPE_EXTRAS=freqtrade`, если это стратегия Freqtrade).
3. Бэктест-фильтр: `hope backtest -c ... --mode candles` (свечные) или `--mode trades` (тиковые/мейкерские),
   неделя истории, 6–15 символов. Убыточные после комиссий с ≥ 100 сделок — в README со статусом `✗ не прошла`,
   в live не запускать.
4. Live paper ≥ 7 суток, для мейкерских — с `store.record_market: true`, затем `hope replay` с вариантами параметров.
5. Зафиксировать в README ветки: источник, логика, параметры, таблицы результатов, статус
   (`⏳ в тестировании` / `✓ рабочая` / `✗ не прошла`), что попробовать дальше.

## 5. Обновление данных

* `hope scan --min-turnover 4000000 --out scan.csv` — актуальный список ликвидных символов и шаг цены в б.п.
* `hope instruments` — кэш точных шагов цены/лота (нужен REST).
* `hope download -s ... --from ... --to ...` — архив сделок для бэктестов (кэш Parquet в `data/history`).

## 6. Известные ограничения

* Paper-исполнение — модель: очередь оценивается по показанному объёму на лучшем уровне (`orderbook.1`), отмены
  впереди нас учитываются только через уменьшение объёма уровня. Реальный fill-rate может быть ниже.
* Бэктест по архиву сделок не знает стакана; бэктест по свечам исполняет только рыночные ордера.
* Фандинг в бэктесте — константа `--funding` (по умолчанию 0.01 % за 8 ч); в live — ставка из тикера.
* Адаптер Freqtrade не поддерживает лимитные входы по entry_pricing, DCA (adjust_trade_position),
  informative-пары через `dp.get_pair_dataframe`, protections кроме CooldownPeriod.
