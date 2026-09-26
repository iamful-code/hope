# Freqtrade в лаборатории hope

Настоящий Freqtrade (dry-run на USDT-перпетуалах Bybit) в Docker плюс экспортёр `hope ft-export`, который через
REST API Freqtrade переносит сделки в БД hope как запуск `engine='freqtrade'`. В мониторе такой запуск выглядит
как обычный: equity, исполнения, круги (FIFO), позиции, события — и сравнивается с запусками движка hope.

Чем отличается от `hope.adapters.freqtrade.runner` (стратегия Freqtrade *внутри* движка hope): здесь работает весь
Freqtrade целиком — его pricing по стакану, `unfilledtimeout`, стопы, DCA, protections. Цена вопроса — доступ к
REST Bybit (гео-блокируется в ряде стран; в этом случае Freqtrade не стартует, а runner — работает).

```
 user_data/strategies/MyStrategy.py
            │
            ▼
 freqtrade trade (dry-run, Bybit) ──► REST API :8080 ──► hope ft-export ──► data/hope.db ──► hope monitor :8000
   tradesv3.dryrun.sqlite                 (FreqUI)         (опрос раз в 10 с)     engine=freqtrade
```

## Быстрый старт (Docker)

```bash
cd adapters/freqtrade
cp .env.example .env                      # FT_STRATEGY, FT_API_USER, FT_API_PASSWORD, FT_JWT_SECRET (>= 32 символов)
cp /путь/к/MyStrategy.py user_data/strategies/
docker compose up -d --build              # монитор: http://localhost:8000, FreqUI: http://127.0.0.1:8080
docker compose logs -f freqtrade ft-exporter
```

* `FT_STRATEGY` — имя **класса** стратегии (`class MyStrategy(IStrategy)`), файл — в `user_data/strategies/`.
* Секреты API не дублируются в `config.json`: Freqtrade читает переопределения из окружения
  (`FREQTRADE__API_SERVER__USERNAME`, `..._PASSWORD`, `..._JWT_SECRET_KEY`, `..._WS_TOKEN`, `FREQTRADE__BOT_NAME`), compose
  прокидывает их из `.env`. JWT-секрет должен быть не короче 32 символов и не чисто числовым (иначе Freqtrade приведёт
  его к числу).
* БД hope монтируется из `../../data` (общая с корневым `docker-compose.yml`), поэтому запуски Freqtrade и движка hope
  видны в одном мониторе. Контейнер монитора здесь называется `hope-ft-monitor`; если уже запущен монитор из корня
  репозитория — этот сервис можно не поднимать (`docker compose up -d freqtrade ft-exporter`).
* Стратегии Freqtrade требуют REST Bybit при старте (`load_markets`, leverage tiers) и для стакана при каждом ордере.

Без Docker (Freqtrade установлен локально, `pip install freqtrade`):

```bash
cd adapters/freqtrade
FREQTRADE__API_SERVER__USERNAME=u FREQTRADE__API_SERVER__PASSWORD=p FREQTRADE__API_SERVER__JWT_SECRET_KEY=<32+ символов> \
  freqtrade trade --config user_data/config.json --strategy MyStrategy --userdir user_data
# в другом терминале, из корня репозитория
hope ft-export --url http://127.0.0.1:8080 --user u --password p --db data/hope.db      # до Ctrl-C
hope ft-export --once ...                                                             # один опрос
```

`hope ft-export --help`: `--url`, `--user`/`--password` (или `FT_API_USER`/`FT_API_PASSWORD`), `--db`, `--interval`
(с, по умолчанию 10), `--name` (имя запуска), `--state` (файл состояния), `--once`, `-v`.

## Конфиг `user_data/config.json`

Собран из провалидированного конфига `frameworks/ft_user_data/config_bybit_futures_dryrun.json`:

* `trading_mode: futures`, `margin_mode: isolated`, `dry_run: true`, `dry_run_wallet: 10000`, `stake_amount: 100`,
  `max_open_trades: 5`; `available_capital: 10000` — чтобы Freqtrade считал стартовый капитал ровно равным кошельку
  (иначе `starting_capital = баланс × tradable_balance_ratio`), это число становится `initial_equity` запуска в hope.
* **Нет `timeframe`** и других «strategy override» ключей (`stoploss`, `minimal_roi`, `trailing_*`, `order_types`) —
  они берутся из стратегии; `unfilledtimeout` 10 минут оставлен.
* `entry_pricing`/`exit_pricing` — по стакану (`use_order_book`, `order_book_top: 1`, `price_side: same`).
* `pair_whitelist` — 15 ликвидных USDT-перпов Bybit (`BTC/USDT:USDT` … `1000PEPE/USDT:USDT`; PEPE на Bybit торгуется
  контрактом `1000PEPEUSDT`, в ccxt это `1000PEPE/USDT:USDT`).
* `api_server` включён на `0.0.0.0:8080`, логин/пароль/секреты — заглушки, перекрываемые из окружения.
* `db_url` — sqlite в `user_data/` (compose передаёт свой `--db-url`, он имеет приоритет), `internals.process_throttle_secs: 5`.

## Как экспортёр отображает Freqtrade в схему hope

Экспортёр (`src/hope/adapters/freqtrade/exporter.py`) раз в `--interval` секунд читает `/api/v1/show_config`, `/status`
(открытые сделки), `/trades` (закрытые, постранично, новые первыми — останавливается на странице из одних уже известных),
`/profit`, при создании запуска — ещё `/balance` и `/whitelist`. Пишет только новое; состояние (run_id, экспортированные
ордера, счётчик `seq`, позиции, пик equity) хранит в `<db>.ftexport.json`, так что рестарт продолжает тот же запуск.

| Freqtrade | hope |
|---|---|
| первый успешный `show_config` | `runs`: `engine=freqtrade`, `name=ft:<strategy>:<bot_name>` (или `--name`), `strategy`, `mode=live` при `dry_run` / `live-real`, `initial_equity=balance.starting_capital` (иначе `balance.total`), `symbols` = whitelist (`BTC/USDT:USDT` → `BTCUSDT`), `config_json` = ответ `show_config`, `started_ts=profit.bot_start_timestamp` |
| ордер сделки с `filled > 0`, уже не открытый (`is_open=false`) | `fills`: `side` по `ft_order_side` (`stoploss` → сторона выхода, `purpose=stop`), `price = average ∨ price`, `qty = filled`, `fee = qty·price·fee_open` для входа / `fee_close` для выхода, `ts = order_filled_timestamp`, `is_maker = (order_type == limit)`, `purpose` entry/exit по `ft_is_entry`, `tag = ft_order_tag ∨ enter_tag/exit_reason`, `seq` — сквозной счётчик, `order_id` — целочисленный номер (id Freqtrade строковые), `inventory_before`/`position_after` — позиция по символу, которую ведёт экспортёр |
| то же | `orders`: `status=filled` (`closed`) или `cancelled` (частично исполненный и отменённый), `qty=amount`, `filled`, `taker = не limit` |
| закрытая сделка | `fills.realized_pnl` ордеров выхода: «грязный» PnL сделки `profit_abs + комиссии − funding_fees` делится между ордерами выхода пропорционально их объёму; частичные выходы ещё открытой сделки считаются по средней цене позиции, при закрытии остаток корректируется так, чтобы сумма сошлась с Freqtrade |
| закрытая сделка | `events` (`info`): «закрыта сделка #id пара long/short: pnl (%), выход: exit_reason, вход: enter_tag» с временем закрытия |
| каждый опрос, `/profit` | `equity`: `equity = initial_equity + profit_all_coin`, `realized_pnl` = сумма грязного PnL экспортированных исполнений, `fees` = сумма их комиссий, `funding` = фандинг закрытых + открытых сделок, `unrealized_pnl` выводится из тождества `equity = initial + realized − fees + funding + unrealized`, `gross_notional`/`n_positions` по открытым сделкам, `n_open_orders` — открытые ордера, `max_drawdown` по пикам equity, наблюдённым экспортёром |
| открытая сделка (`/status`, `amount > 0`) | `positions`: `qty` со знаком (`is_short` → отрицательная), `avg_price=open_rate`, `mark=current_rate`, `unrealized_pnl=profit_abs`, `funding=funding_fees`, `opened_ts=open_fill_timestamp`; символы, открытые на прошлом опросе и закрытые теперь, получают строку с `qty=0` |
| подключение / потеря связи | `events` `info` / `warning` |
| `Ctrl-C` экспортёра | `runs.status=stopped`, `finished_ts`; следующий старт с тем же состоянием возвращает `running` |

Монитор считает запуск «живым» по свежести последнего снимка `equity` — он пишется каждым опросом, даже без сделок.

## Ограничения

* **Нет markout, bid/ask, очереди** — REST Freqtrade не отдаёт стакан на момент исполнения; вкладки markout/спред пусты.
  Сравнивать с hope-запусками нужно по PnL, кругам и комиссиям.
* **Комиссии — оценка** `qty·price·fee_open/fee_close` (ставки, которые Freqtrade получил от ccxt/биржи; в dry-run —
  maker/taker из описания рынка). Комиссия в базовой валюте (`ft_fee_base`, только spot) не учитывается.
* **`realized_pnl` по ордерам выхода — пропорциональная доля** PnL сделки, а не поордерный расчёт; в сумме по сделке
  совпадает с Freqtrade. FIFO-круги монитора строятся по самим исполнениям и от этого поля не зависят.
* `positions.unrealized_pnl` = `profit_abs` Freqtrade (нетто, с оценочной комиссией выхода), а не `(mark − avg)·qty`.
* Частично исполненный **открытый** ордер экспортируется после его закрытия/отмены (у Freqtrade нет времени частичного
  исполнения); в dry-run частичных исполнений не бывает.
* `contract_size ≠ 1` не учитывается (у USDT-перпов Bybit он равен 1); фандинг берётся из `trade.funding_fees`.
* Кривая equity начинается с момента запуска экспортёра; сделки, закрытые до этого, попадают в `fills`/`events`
  своим временем, но снимков equity за прошлое нет.
* Один экспортёр = один бот; смена `bot_name` или стратегии в том же файле состояния создаёт новый запуск.

## Несколько ботов одновременно

Один процесс `freqtrade trade` — одна стратегия. Для N стратегий: N сервисов `freqtrade` с разными `--db-url`,
`--logfile`, портами наружу и `FREQTRADE__BOT_NAME`, и N экспортёров с разными `--state` (в одну БД hope) либо с разными
`--db` (монитор умеет несколько: `hope monitor --db data/a.db,data/b.db`).

```yaml
services:
  ft-b:
    image: freqtradeorg/freqtrade:stable
    volumes: ["./user_data:/freqtrade/user_data"]
    ports: ["127.0.0.1:8081:8080"]
    environment:
      - FREQTRADE__API_SERVER__USERNAME=${FT_API_USER}
      - FREQTRADE__API_SERVER__PASSWORD=${FT_API_PASSWORD}
      - FREQTRADE__API_SERVER__JWT_SECRET_KEY=${FT_JWT_SECRET}
      - FREQTRADE__BOT_NAME=hope-ft-b
    command: >
      trade --config /freqtrade/user_data/config.json --strategy StrategyB
      --db-url sqlite:////freqtrade/user_data/tradesv3.b.sqlite --logfile /freqtrade/user_data/logs/b.log
  ft-exporter-b:
    build: ../..
    command: ["ft-export", "--url", "http://ft-b:8080", "--db", "/app/data/hope.db",
              "--state", "/app/data/hope.db.ftexport-b.json"]
    environment: ["FT_API_USER=${FT_API_USER}", "FT_API_PASSWORD=${FT_API_PASSWORD}"]
    volumes: ["../../data:/app/data"]
```

## Бэктест и загрузка истории на своей машине

Бэктест Freqtrade запускается там, где есть доступ к REST Bybit (нужен `load_markets`). Данные складываются в
`user_data/data/bybit/futures/<PAIR>-<tf>-futures.feather` плюс `-mark` и `-funding_rate` (для futures качаются сами).

```bash
cd adapters/freqtrade
# история: все пары из whitelist, 5m и 1h, с 2024-01-01 по сегодня
freqtrade download-data --config user_data/config.json --userdir user_data \
  --exchange bybit --trading-mode futures --timeframes 5m 1h --timerange 20240101-
# или отдельные пары
freqtrade download-data --userdir user_data --exchange bybit --trading-mode futures \
  --pairs BTC/USDT:USDT ETH/USDT:USDT --timeframes 5m --timerange 20240101-20240630

# бэктест (таймфрейм — из стратегии, т.к. в конфиге его нет; --timeframe перекрывает)
freqtrade backtesting --config user_data/config.json --userdir user_data \
  --strategy MyStrategy --timerange 20240101-20240630 --export trades
freqtrade backtesting-show --userdir user_data          # последний результат
```

В Docker то же самое: `docker compose run --rm freqtrade download-data ...` / `... backtesting ...` (сервис `freqtrade`
использует тот же `user_data`). Для spot-стратегий поменяйте `trading_mode: spot`, уберите `margin_mode` и используйте
пары вида `BTC/USDT`.
