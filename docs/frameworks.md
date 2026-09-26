# Фреймворки для real-time paper-trading на Bybit (Python 3.11) — результаты проверки

Дата: 2026-09-26. Проверка «руками»: клонированы репозитории (`--depth 1`), установлены пакеты в `uv venv` (Python 3.11.15),
прочитан исходный код, написаны пробные скрипты. Рабочая папка: `scratchpad/frameworks/`
(`freqtrade-src/`, `hummingbot-src/`, `nautilus-src/` (master = v2), `nautilus-v1-src/` (ветка `develop_v1`), `.venv/`,
`ws_probe*.py`, `nautilus_sandbox_probe.py`, `ft_user_data/`).

Ограничения контейнера: REST Bybit (`api.bybit.com`, `api-demo`, `api-testnet`) гео-блокирован (CloudFront 403);
работает только публичный WebSocket `wss://stream.bybit.com/v5/public/{linear,spot,inverse}` (проверено). Всё, что требует REST,
оценено по исходникам; на машине владельца REST будет доступен.

---

## TL;DR

| | Freqtrade | Hummingbot | Nautilus Trader | pybit / ccxt (свой движок) |
|---|---|---|---|---|
| Версия (PyPI, py3.11) | **2026.8** (`requires-python >=3.11`) | 2.17.0 (conda, `python>=3.10.12`; pip-wheel нет) | **1.221.0** — последняя с поддержкой 3.11; 1.222+ и 2.0.0rc требуют **>=3.12** | pybit 5.17.0, ccxt 4.5.84 |
| Bybit spot | да (`trading_mode: spot`) | да (`bybit`) | да (`-SPOT`) | да |
| Bybit USDT-perp | да (`futures` + `isolated`, one-way) | да (`bybit_perpetual`, linear+inverse) | да (`-LINEAR`, `-INVERSE`, `-OPTION`) | да |
| Paper-trading без ключей | **да** (`dry_run: true`, ключи вычищаются) | да для spot-семантики (`bybit_paper_trade`); **perp paper — фактически нет** | Sandbox есть, но Bybit-адаптер требует ключи для загрузки инструментов (подписанные REST); обход есть (см. ниже) | своё |
| Симуляция исполнения | market: интерполяция по L2-книге (REST), slippage cap 5%; limit: ждёт пересечения best bid/ask; комиссии из ccxt-markets или `fee` | market: проход по книге; limit: заполняется при пересечении best-цены или сделки | `SimulatedExchange`: L1-книга из quotes/bars/OHLC-путь, лимитки по касанию, `MakerTakerFeeModel`, latency 0 | пишете сами |
| Много стратегий | 1 процесс на конфиг (разные `db_url`, порт API) | 1 бот на инсталляцию/контейнер | N стратегий в одном `TradingNode` | как угодно |
| Сложность портирования | низкая (pandas-DataFrame API) | средняя/высокая | высокая (event-driven, типизированные объекты) | средняя (свой каркас) |

Рекомендация: **Freqtrade — основной движок лаборатории** (сигнальные/индикаторные стратегии на свечах, long/short на USDT-перпах,
готовый dry-run + FreqUI + Docker + бэктест на тех же данных). **Nautilus** — для tick/orderbook/HFT-подобных стратегий и
маркет-мейкинга с высокой точностью симуляции, но в **отдельном venv на Python 3.12/3.13** (иначе вы заморожены на 1.221.0 от
2025-10). **Hummingbot** — только если нужны его готовые MM/grid/arb-контроллеры на spot; для перпов paper-режим не работает.
**pybit/ccxt** — для лёгкого собственного «свечного» движка (WS-kline даёт свечи без REST).

---

## 0. Что реально проверено в контейнере (WebSocket Bybit v5)

Скрипт `ws_probe.py` (пакет `websockets` 17.1, `ssl cafile=/root/.ccr/ca-bundle.crt`), вывод в `ws_probe.out`.

`wss://stream.bybit.com/v5/public/linear`, подписка `{"op":"subscribe","args":["kline.1.BTCUSDT","tickers.BTCUSDT","orderbook.1.BTCUSDT","publicTrade.BTCUSDT"]}` — за 10 с:
`kline.1`: 3 сообщения, `tickers`: 56, `orderbook.1`: 94, `publicTrade`: 3 (батчами).

Формы сообщений (реальные):

```json
{"topic":"kline.1.BTCUSDT","type":"snapshot","ts":1790409768441,
 "data":[{"start":1790409720000,"end":1790409779999,"interval":"1","open":"84045","close":"84017.8","high":"84045","low":"84017.8",
          "volume":"7.316","turnover":"614833.7084","confirm":false,"timestamp":1790409768441}]}
```
`confirm:true` приходит при закрытии свечи (проверено на spot: `"confirm":true` в момент `end`). Значит **свечи можно строить без REST**:
брать бар с `confirm==true` (или самому агрегировать `publicTrade`). История для «прогрева» индикаторов — REST `/v5/market/kline`
(публичный, без ключа) либо локальный кеш.

```json
{"topic":"tickers.BTCUSDT","type":"snapshot","data":{"symbol":"BTCUSDT","lastPrice":"84021.20","markPrice":"84022.62","indexPrice":"84061.19",
 "bid1Price":"84021.20","bid1Size":"0.646","ask1Price":"84021.30","ask1Size":"8.530","fundingRate":"0.00007162","nextFundingTime":"1790438400000",
 "openInterest":"58183.909","volume24h":"51085.9410","turnover24h":"4295596438.8240", ...},"cs":816281664756,"ts":1790409768183}
{"topic":"tickers.BTCUSDT","type":"delta","data":{"symbol":"BTCUSDT","markPrice":"84022.58","openInterestValue":"..."},"cs":...,"ts":...}
```
(linear-тикер: после `snapshot` идут `delta` только с изменившимися полями — нужно мержить состояние.)

```json
{"topic":"orderbook.1.BTCUSDT","type":"snapshot","ts":1790409768609,"data":{"s":"BTCUSDT","b":[["84017.5","1.777"]],"a":[["84017.6","2.725"]],"u":97687715,"seq":816281667578},"cts":1790409768607}
{"topic":"publicTrade.BTCUSDT","type":"snapshot","ts":1790409769488,"data":[{"T":1790409769487,"s":"BTCUSDT","S":"Sell","v":"0.001","p":"84017.50","L":"MinusTick","i":"865f1dcd-...","BT":false,"RPI":false,"seq":816281670081}, ...]}
```

`wss://stream.bybit.com/v5/public/spot` — `kline.1.BTCUSDT` и `tickers.BTCUSDT` работают; **spot-тикер не содержит bid/ask**
(`lastPrice, highPrice24h, lowPrice24h, prevPrice24h, volume24h, turnover24h, price24hPcnt, usdIndexPrice`) — для BBO на споте подписывайтесь на `orderbook.1`.
`wss://stream.bybit.com/v5/public/inverse` — `tickers.BTCUSD` работает.

Дополнительно проверено: `pybit.unified_trading.WebSocket(channel_type="linear")` + `kline_stream(1,"BTCUSDT",cb)` + `ticker_stream` — работает
(`ws_probe2.py`); `ccxt.pro.bybit().watch_ohlcv("BTC/USDT:USDT","1m")`, `watch_order_book`, `watch_ticker` — работают **без REST**, если
подсунуть рынки через `exchange.set_markets({...})` (`ws_probe3.py`; потребовался явный `aiohttp.ClientSession` с SSL-контекстом прокси — особенность контейнера).

---

## 1. Freqtrade (`freqtrade/freqtrade`)

**Версия/питон.** PyPI `freqtrade==2026.8` (установлено, `freqtrade-client==2026.8`); master `2026.9-dev` (коммит `c0a991f5`, 2026-09-24).
`pyproject.toml`: `requires-python = ">=3.11"`, классификаторы 3.11–3.14. Зависимости ставятся wheel'ами (`ta-lib==0.7.1` wheel, `ccxt>=4.5.76`).
Официальный Docker-образ `freqtradeorg/freqtrade:stable` (также `:develop`, `:stable_plot`, `:stable_freqai`), базовый образ `python:3.14.7-slim-trixie`.

**Поддержка Bybit** (`freqtrade/exchange/bybit.py`):
- `_supported_trading_mode_margin_pairs = [(SPOT, NONE), (FUTURES, ISOLATED)]` — cross закомментирован; inverse-контракты не поддерживаются
  (`dry_run_liquidation_price` бросает исключение для `market["inverse"]`). Класс `BybitEU` — только spot.
- `_ccxt_config`: spot → `options.defaultType=spot`; futures → `options.defaultSettle=<stake_currency>` (USDT/USDC).
- `additional_exchange_init` (только **не** в dry-run): `set_position_mode(False)` → **One-way mode для всего (суб)аккаунта**; `is_unified_enabled()` → флаг
  `unified_account`. Документация: «Freqtrade считает аккаунт выделенным под бота; используйте по субаккаунту на бота, особенно с UTA».
- В ордерах futures добавляется `position_idx=0`; стоп-ордера через `privatePostV5OrderCreate` (обход бага ccxt); `stoploss_on_exchange` только futures
  (`stop-limit`/`stop-market`, trigger `LastPrice/MarkPrice/IndexPrice`); `order_time_in_force`: GTC/FOK/IOC/PO.
- Funding: Bybit не отдаёт «применённый» funding по позиции → `get_funding_fees` считает по истории funding rate + mark-свечам (и в live тоже).
  Значения по умолчанию `mark_ohlcv_timeframe=1h`, `funding_fee_timeframe=1h`, `funding_fee_candle_limit=200`.
- Leverage tiers: `fetch_leverage_tiers` (REST, пагинация), кеш на 1 день в `user_data/data/bybit/futures/leverage_tiers_USDT.json`. Загружаются при
  старте бота в futures даже в dry-run (`load_leverage_tiers=True` в `freqtradebot.py`).
- Классический (не-unified) spot-аккаунт: `fetchOrder` отключён (эмулируется), market-buy требует цену. Есть `has_delisting` (по `deliveryTime`).
- Демо-режим Bybit: `"exchange": {"demo_trading": true}` + отдельные demo-ключи — **несовместим с `dry_run`** (это «реальные» сделки на demo-счёте Bybit).

**Dry-run (симуляция)** — `freqtrade/exchange/exchange.py`:
- API-ключи **не нужны**: при `dry_run` они удаляются из конфига (`remove_exchange_credentials`), кроме бирж с `always_require_api_keys` (luno, modetrade — не Bybit).
  Выполняются только read-only вызовы: `load_markets`, OHLCV (в live-режиме свечи через ccxt.pro `watchOHLCV`, т.к. `ws_enabled: True`, `enable_ws` по умолчанию `true`),
  `fetch_l2_order_book`, тикеры, leverage tiers, funding history. Вызовы `set_leverage`/`set_margin_mode`/`set_position_mode` в dry-run пропускаются.
- `create_dry_run_order`: объём округляется по precision/contract size; запрашивается стакан `fetch_l2_order_book(pair, 20)`.
  - **Limit**, пересекающий спред более чем на 1% (`allowed_diff=0.01`), конвертируется в market.
  - **Market**: цена исполнения — интерполяция по стакану (`get_dry_market_fill_price`, проход по asks/bids с учётом объёма), ограничение проскальзывания
    `worst_rate = rate*(1±0.05)`; статус сразу `closed`, комиссия **taker**.
  - **Limit**: остаётся `open`; при каждом `fetch_order` (каждый цикл бота, `internals.process_throttle_secs`) проверяется `_dry_is_price_crossed`
    (buy: `best_ask <= limit`, sell: `best_bid >= limit`) → полное заполнение по цене лимита, комиссия **maker** (если заполнилось сразу при создании — taker).
    Частичных заполнений нет. Незаполненные ордера отменяются по `unfilledtimeout`.
  - **Stoploss** (dry): срабатывает при пересечении стоп-цены, цена — интерполяция по стакану с worst_rate = цена ордера; создание стопа, который сработал бы сразу, → `InvalidOrderException`.
- Комиссия: `config.fee` (если задана) иначе `ccxt.calculate_fee(...)["rate"]` из данных рынка (для Bybit берётся из markets), иначе `fees.trading` по умолчанию; если ничего нет — 0 и warning.
- Кошелёк: `dry_run_wallet` (float или dict по валютам, default 1000), `tradable_balance_ratio`. Открытые dry-ордера переживают рестарт (считаются незаполненными).
- Ликвидация: считается формулой Bybit isolated (`dry_run_liquidation_price`), `liquidation_buffer` 0.05.

**FreqUI + REST API.** Блок `api_server` (см. конфиг ниже). Валидация схемы: `jwt_secret_key` **минимум 32 символа** (проверено — конфиг с коротким ключом
отвергается). UI ставится `freqtrade install-ui` (в Docker-образе уже есть) и доступен на `http://127.0.0.1:8080` при запущенном `freqtrade trade`.
В Docker слушать `0.0.0.0`, порт мапить `127.0.0.1:8080:8080`. `ws_token` — для WS-канала UI/внешних продюсеров.

**Несколько стратегий одновременно** (`docs/advanced-setup.md`): один процесс `freqtrade trade` на одну стратегию/конфиг; обязательно разные `db_url`,
разные `api_server.listen_port`, разные Telegram-боты (если включён). В compose — N сервисов с общим `user_data` (или отдельные), см. сниппет ниже.
Нельзя: несколько ботов на один реальный (суб)аккаунт; в dry-run это неважно.

**Исторические данные / бэктест.** `freqtrade download-data --exchange bybit --trading-mode futures --pairs BTC/USDT:USDT ETH/USDT:USDT --timeframes 5m 1h --timerange 20240101-`
(Bybit: `ohlcv_has_history: True`; для futures автоматически качаются `mark` и `funding_rate` свечи). Формат по умолчанию feather, раскладка
`user_data/data/<exchange>/futures/BTC_USDT_USDT-5m-futures.feather`, `...-1h-mark.feather`, `...-1h-funding_rate.feather`
(`_pair_data_filename`: биржа только в имени каталога). Бэктест: `freqtrade backtesting -c config.json --strategy X --timerange ...`.

Данные Binance для стратегии под Bybit: технически да — раскладка файлов биржа-независима, достаточно `--datadir user_data/data/binance`
при конфиге с `exchange.name: bybit` (или просто скачать с binance в тот же datadir). Нюансы: бэктест всё равно инициализирует биржу из конфига
(`load_markets`, leverage tiers, комиссии — с Bybit), funding/mark будут бинансовские (интервалы 1h у обоих в текущем коде), список пар должен существовать на Bybit.
Практичнее качать историю прямо с Bybit — она есть.

**Интерфейс стратегии для портирования** (`freqtrade/strategy/interface.py`, `INTERFACE_VERSION = 3`):
- Обязательное: `timeframe`, `stoploss`, `minimal_roi`, `populate_indicators(df, metadata)`, `populate_entry_trend` (колонки `enter_long`, `enter_short`, `enter_tag`),
  `populate_exit_trend` (`exit_long`, `exit_short`, `exit_tag`). `can_short = True` — для шортов на futures (на spot игнорируется).
- `startup_candle_count`, `process_only_new_candles=True`, `use_exit_signal`, `exit_profit_only`, `ignore_roi_if_entry_signal`, `order_types`, `order_time_in_force`.
- Стоп/выход: `trailing_stop*`, `use_custom_stoploss=True` + `custom_stoploss(self, pair, trade, current_time, current_rate, current_profit, after_fill, **kwargs) -> float|None`
  (возвращает относительное расстояние от `current_rate`, не может быть ниже `self.stoploss`), `custom_exit(...)`, `custom_roi(...)`, хелперы `stoploss_from_open`, `stoploss_from_absolute`.
- Цены/размер: `custom_entry_price`, `custom_exit_price`, `custom_stake_amount`, `adjust_trade_position` (DCA, `position_adjustment_enable=True`),
  `adjust_entry_price`/`adjust_exit_price`, `leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs)` (только futures).
- Колбэки: `bot_start`, `bot_loop_start`, `confirm_trade_entry/exit`, `order_filled`, `check_entry_timeout/check_exit_timeout`, `lock_pair`.
- Informative: `informative_pairs()` → `[("BTC/USDT:USDT","1h"), ...]` + ручной `merge_informative_pair`, либо декоратор `@informative('1h')` / `@informative('1h','BTC/USDT:USDT')`
  над `populate_indicators_*` (колонки `rsi_1h`, `btc_usdt_close_1h`).
- **Gotcha для лаборатории**: параметры «Strategy Override» (`stoploss`, `minimal_roi`, `timeframe`, `trailing_*`, `order_types`, `unfilledtimeout`, …), заданные в **config.json**,
  **перекрывают** значения стратегии. В конфиге для батча стратегий их лучше не задавать (в сниппете ниже их нет — они берутся из стратегии; схема требует
  `stoploss` только после слияния со стратегией, проверено через `StrategyResolver`).

**Минимальный config.json — Bybit USDT-perp dry-run, 5m** (файл `ft_user_data/config_bybit_futures_dryrun.json`, провалидирован
`Configuration` + `StrategyResolver` + `validate_config_consistency` офлайн; `freqtrade trade` при старте потребует REST Bybit для `load_markets`):

```json
{
  "max_open_trades": 3,
  "stake_currency": "USDT",
  "stake_amount": 100,
  "tradable_balance_ratio": 0.99,
  "fiat_display_currency": "USD",
  "dry_run": true,
  "dry_run_wallet": 10000,
  "cancel_open_orders_on_exit": false,
  "trading_mode": "futures",
  "margin_mode": "isolated",
  "timeframe": "5m",
  "unfilledtimeout": {"entry": 10, "exit": 10, "unit": "minutes"},
  "entry_pricing": {"price_side": "same", "use_order_book": true, "order_book_top": 1,
                    "price_last_balance": 0.0, "check_depth_of_market": {"enabled": false, "bids_to_ask_delta": 1}},
  "exit_pricing": {"price_side": "same", "use_order_book": true, "order_book_top": 1},
  "exchange": {
    "name": "bybit",
    "key": "",
    "secret": "",
    "ccxt_config": {},
    "ccxt_async_config": {},
    "pair_whitelist": ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"],
    "pair_blacklist": []
  },
  "pairlists": [{"method": "StaticPairList"}],
  "telegram": {"enabled": false, "token": "", "chat_id": ""},
  "api_server": {
    "enabled": true,
    "listen_ip_address": "0.0.0.0",
    "listen_port": 8080,
    "verbosity": "error",
    "enable_openapi": false,
    "jwt_secret_key": "CHANGE_ME_to_a_random_string_of_32_plus_chars",
    "CORS_origins": [],
    "username": "freqtrader",
    "password": "CHANGE_ME",
    "ws_token": "CHANGE_ME_ws_token"
  },
  "bot_name": "bybit_futures_dry_5m",
  "db_url": "sqlite:///user_data/tradesv3.bybit_futures_dry.sqlite",
  "initial_state": "running",
  "force_entry_enable": false,
  "internals": {"process_throttle_secs": 5}
}
```
Примечания: `timeframe` в конфиге тоже перекрывает стратегию — уберите, если стратегии должны жить на своих ТФ. Для spot: `"trading_mode": "spot"`, без `margin_mode`,
пары `BTC/USDT`. Для demo-счёта Bybit: `"dry_run": false`, `"exchange": {"demo_trading": true, "key": ..., "secret": ...}`.

**docker-compose для нескольких ботов** (шаблон):
```yaml
services:
  ft_strat_a:
    image: freqtradeorg/freqtrade:stable
    restart: unless-stopped
    volumes: ["./user_data:/freqtrade/user_data"]
    ports: ["127.0.0.1:8081:8080"]
    command: >
      trade --logfile /freqtrade/user_data/logs/strat_a.log
      --db-url sqlite:////freqtrade/user_data/strat_a.dry.sqlite
      --config /freqtrade/user_data/config_bybit_futures_dryrun.json
      --strategy StratA
  ft_strat_b:
    image: freqtradeorg/freqtrade:stable
    restart: unless-stopped
    volumes: ["./user_data:/freqtrade/user_data"]
    ports: ["127.0.0.1:8082:8080"]
    command: >
      trade --logfile /freqtrade/user_data/logs/strat_b.log
      --db-url sqlite:////freqtrade/user_data/strat_b.dry.sqlite
      --config /freqtrade/user_data/config_bybit_futures_dryrun.json
      --strategy StratB
```
(`--db-url` в CLI перекрывает `db_url` конфига; порт внутри контейнера один и тот же 8080, наружу разные.)

---

## 2. Hummingbot (`hummingbot/hummingbot`)

**Версия/установка.** `hummingbot/VERSION` = **2.17.0**; `setup.py: python_requires=">=3.10.12"`, `setup/environment.yml: python>=3.10.12`.
Установка только через conda (`./install` → `conda env create`, затем `./compile` — Cython) или Docker `hummingbot/hummingbot:latest`
(база `continuumio/miniconda3`). Зависимости тяжёлые: ~67 conda-пакетов (numba, pandas, ta-lib, web3, xrpl-py, scalecodec, rust toolchain, …) +
pip (`injective-py`, `aptos-sdk`, `solders`, `lighter-sdk`, `decibel-python-sdk`). В контейнере не устанавливал (pip-wheel на PyPI нет; conda-окружение — гигабайты);
на 3.11 совместим по декларации.

**Коннекторы Bybit.**
- `hummingbot/connector/exchange/bybit` (spot): REST/WS v5 (`wss://stream.bybit.com/v5/public/spot`, private `/v5/private`), домены `bybit_main`/`bybit_testnet`.
  Тесты в репо — только `test_bybit_api_order_book_data_source.py` (зрелость ниже, чем у perp).
- `hummingbot/connector/derivative/bybit_perpetual`: v5, `WSS_LINEAR_PUBLIC_URLS=/v5/public/linear`, `WSS_NON_LINEAR=/v5/public/inverse`, домены
  `bybit_perpetual_main`/`bybit_perpetual_testnet`; `supported_position_modes = [ONEWAY, HEDGE]`, `positionIdx` выставляется по режиму; баланс — `accountType=UNIFIED`
  (т.е. рассчитан на UTA). 6 тестовых модулей. Демо-домена (`api-demo`) нет — только mainnet/testnet.

**Paper trade.** `conf/conf_client.yml` → `paper_trade.paper_trade_exchanges` (default `[binance, kucoin, kraken, gate_io]`) и
`paper_trade_account_balance` (default `{"BTC":1,"USDT":100000,"USDC":100000,"ETH":20,...}`). В список можно добавить `bybit` (и формально `bybit_perpetual`) —
тогда появляется коннектор `bybit_paper_trade` (`ConnectorManager.create_connector` → `create_paper_trade_market(base, pairs)`), который берёт **реальный
`OrderBookTracker` базового коннектора** (публичные WS, без ключей) и симулирует биржу в `PaperTradeExchange(ExchangeBase)`
(`hummingbot/connector/exchange/paper_trade/paper_trade_exchange.pyx`):
- market-ордера: `order_book.simulate_buy/sell(amount)` — проход по книге, комиссия `build_trade_fee(is_maker=False)`;
- limit-ордера: заполняются целиком, когда противоположная лучшая цена пересекает лимит (`c_process_crossed_limit_orders`, каждый тик) или когда публичная
  сделка проходит через цену (`c_match_trade_to_limit_orders`), комиссия maker; отмена, если не хватает баланса; нет очереди, латентности, частичных заполнений.
- **Перпы в paper-режиме не поддерживаются**: `PaperTradeExchange` — spot-семантика (балансы base/quote), без `position_mode`, leverage, позиций, funding;
  при этом executors определяют «перп» по подстроке `perpetual` в имени коннектора (`ExecutorBase.is_perpetual_connector`) и обращаются к `connector.position_mode`,
  `PerpetualOrderCandidate` → упадёт. Для Bybit-перпов в Hummingbot нужен реальный счёт: `bybit_perpetual_testnet` (testnet-ключи).

**Headless / Docker.** `bin/hummingbot_quickstart.py -f <strategy_or_script.yml> -p <password> --headless` (или env `HEADLESS_MODE=true`, `HBOT_PASSWORD`);
новый неинтерактивный CLI `hbot` (`hummingbot/cli`, typer): `hbot connect bybit`, `hbot create/import`, `hbot start <config> [--foreground]`, `hbot stop`, `hbot status`,
`hbot logs -f`, `hbot history`, `hbot config paper_trade_exchanges ...`. **Один бот на инсталляцию/контейнер** (`--replace` для замены). В `docker-compose.yml`
тома `conf/`, `conf/connectors`, `conf/strategies`, `conf/controllers`, `conf/scripts`, `logs/`, `data/`, `scripts/`, `controllers/`; управление
`docker exec hummingbot hbot start <config>`; `network_mode: host`.

**Хранение сделок.** SQLite `data/<имя_конфига>.sqlite` (модели `hummingbot/model/`: `TradeFill` — `config_file_path, strategy, market, symbol, base_asset, quote_asset,
timestamp, order_id, trade_type, order_type, price, amount, leverage, trade_fee(JSON), trade_fee_in_quote, exchange_trade_id, position`; также `Order`, `OrderStatus`,
`Position`, `Controllers`, `Executors`). `db_engine` в `conf_client.yml`: sqlite (default) или внешняя БД.

**Типы стратегий.** V1 (`hummingbot/strategy/`): `pure_market_making`, `avellaneda_market_making`, `perpetual_market_making`, `cross_exchange_market_making`,
`amm_arb`, `spot_perpetual_arbitrage`, `liquidity_mining`, `hedge`, `cross_exchange_mining`. V2: скрипты `scripts/*.py` (`ScriptStrategyBase`, например `simple_pmm.py`,
`simple_vwap.py`, `v2_with_controllers.py`, `v2_funding_rate_arb.py`) и контроллеры `controllers/` — `directional_trading/` (`bollinger_v1/v2`, `macd_bb_v1`, `supertrend_v1`,
`dman_v3`, `bollingrid`), `market_making/` (`pmm_simple`, `pmm_dynamic`, `dman_maker_v2`), `generic/` (`grid_strike`, `multi_grid_strike`, `arbitrage_controller`, `stat_arb`,
`xemm_multiple_levels`, `pmm_v1`, …) поверх executors (`PositionExecutor` с triple-barrier, DCA, grid, arbitrage, XEMM). Есть `strategy_v2/backtesting`.

**Вердикт.** Для батча «свечных» стратегий с GitHub — слишком тяжёлый и чужой по модели (executors/controllers, conda, один бот на контейнер, paper только spot).
Уместен точечно для MM/grid/арбитражных контроллеров на **spot** Bybit в paper-режиме или на perp-testnet с ключами.

---

## 3. Nautilus Trader (`nautechsystems/nautilus_trader`)

**Версии/питон — главный gotcha.**
- master = **v2.0.0rc6** (Rust core + PyO3, пакет в `python/`, `requires-python >=3.12,<3.15`); ветка `develop_v1` = **1.231.0** (тоже `>=3.12`).
- PyPI: 1.222.0 (2026-01-02) и новее — только `>=3.12`; **последняя версия для Python 3.11 — `nautilus_trader==1.221.0` (2025-10-26)**. Именно она установилась
  (wheel `cp311-manylinux`). Т.е. на 3.11 вы получаете 11-месячный код без обновлений. Рекомендация: отдельный venv на **3.12/3.13** (1.231 v1 или `--pre` 2.0.0rc);
  API 1.221 → 1.231 немного отличается (например, в 1.231 `BybitDataClientConfig(environment=BybitEnvironment.MAINNET)`, в 1.221 — флаги `demo`/`testnet`).
- v1 (Cython) и v2 (PyO3) устанавливаются под одним именем — не смешивать в одном venv.

Ниже — по **установленной 1.221.0** (`.venv/lib/python3.11/site-packages/nautilus_trader/adapters/bybit`, `adapters/sandbox`).

**Bybit-адаптер.** Типы продуктов `BybitProductType = SPOT | LINEAR | INVERSE | OPTION`; символика `BTCUSDT-LINEAR.BYBIT`, `BTCUSDT-SPOT.BYBIT`, `BTCUSD-INVERSE.BYBIT`.
`BybitDataClientConfig(api_key, api_secret, product_types, base_url_http, demo, testnet, update_instruments_interval_mins=60, recv_window_ms=5000, bars_timestamp_on_close=True)`;
`BybitExecClientConfig(..., futures_leverages: dict[BybitSymbol,int], position_mode: dict[BybitSymbol, BybitPositionMode], margin_mode, use_ws_trade_api, use_http_batch_api, …)`.
- Загрузка инструментов (`BybitInstrumentProvider.load_all_async/load_ids_async`) делает REST: `/v5/asset/coin/query-info` (**подписанный**), `/v5/market/instruments-info`
  (публичный), `/v5/account/fee-rate` (**подписанный**). Поэтому **даже data-only клиент требует API-ключи**: фабрика `get_bybit_http_client` при отсутствии `api_key`
  в конфиге читает `BYBIT_API_KEY`/`BYBIT_API_SECRET` (`BYBIT_DEMO_*`, `BYBIT_TESTNET_*`) и бросает `RuntimeError`, если переменной нет.
- Обход без REST/ключей (проверено в `nautilus_sandbox_probe.py`): `InstrumentProviderConfig(load_all=False)` (тогда `initialize()` лишь пишет warning и ничего не грузит),
  `api_key="dummy", api_secret="dummy"` (не используются), `update_instruments_interval_mins=None`, и после `node.build()` вручную
  `node.kernel.cache.add_instrument(CryptoPerpetual(...))` с precision/increment/комиссиями (maker/taker нужны `MakerTakerFeeModel`). Результат: `ExecEngine.check_connected() == True`
  (sandbox подхватил инструмент из кеша), data-client пошёл сразу на WS без единого REST-вызова. Сам WS в контейнере не поднялся из-за TLS-перехвата
  прокси (`invalid peer certificate: UnknownIssuer`): Rust-клиент использует вшитые `webpki-roots`, а `WebSocketConfig` в 1.221 не имеет `certs_dir` — на машине владельца
  этой проблемы нет.
- WS-данные: `subscribe_bars(BarType("BTCUSDT-LINEAR.BYBIT-5-MINUTE-LAST-EXTERNAL"))` → топик `kline.5.BTCUSDT`, бар публикуется **только при `confirm==true`**
  (незакрытые свечи пропускаются), `ts_event` на закрытие бара при `bars_timestamp_on_close=True` (это требование bar-execution в sandbox). `subscribe_quote_ticks` → `orderbook.1`
  (для spot — `tickers`), `subscribe_trade_ticks` → `publicTrade`, `subscribe_order_book_deltas` → `orderbook.{depth}`, есть funding-rate/mark/index price. Прогрев:
  `request_bars(bar_type, limit=...)` → REST `/v5/market/kline` (публичный).

**Sandbox-исполнение** (`adapters/sandbox/execution.py`, `config.py`):
`SandboxExecutionClientConfig(venue="BYBIT", starting_balances=["10_000 USDT"], base_currency="USDT", oms_type="NETTING", account_type="MARGIN",
default_leverage=Decimal(1), leverages={...}, book_type="L1_MBP", bar_execution=True, trade_execution=False, reject_stop_orders=True, support_gtd_orders=True,
support_contingent_orders=True, use_position_ids=True, use_reduce_only=True, frozen_account=False)`.
- Внутри — тот же `SimulatedExchange` + `BacktestExecClient`, что в бэктесте: подписка на шину `data.*.{venue}.*`, каждый `QuoteTick`/`TradeTick`/`Bar`/`OrderBookDeltas`/`Depth10`
  → `exchange.process_*` → `exchange.process(ts)` (матчинг сразу, без очереди).
- Захардкожено: `FillModel()` (default `prob_fill_on_limit=1.0`, `prob_slippage=0.0` — лимитка заполняется при касании, проскальзывания на L1 нет),
  `MakerTakerFeeModel()` (комиссии из `instrument.maker_fee/taker_fee`), `LatencyModel(0)`. Через конфиг сменить нельзя (нужно наследовать клиент).
- С `book_type="L1_MBP"` книга — только top-of-book из quote-тиков/баров: market-ордер исполняется по best bid/ask **без учёта глубины**; bar-execution превращает
  каждый внешний бар в путь O→H→L→C (4 синтетических апдейта, объём делится на 4) — стопы/лимиты внутри бара срабатывают по этому пути. Для учёта глубины
  подписывайтесь на `orderbook.{50}` и ставьте `book_type="L2_MBP"` (тогда bar-execution не работает по правилам движка).
- Отчёты `generate_*_reports` пустые → reconciliation при старте тривиальна. Позиции/маржа считаются по `margin_init/margin_maint` инструмента (в реальном
  провайдере берутся из risk-limits Bybit).

**API стратегии** (`trading/strategy.pxd`, `common/actor.pxd`): класс `Strategy(StrategyConfig)`; хендлеры `on_start/on_stop/on_reset`, `on_bar(Bar)`, `on_quote_tick(QuoteTick)`,
`on_trade_tick(TradeTick)`, `on_order_book_deltas`, `on_order_book`, `on_instrument`, `on_funding_rate/on_mark_price/on_index_price`, `on_historical_data`, события
`on_order_filled/accepted/rejected/canceled/expired/...`, `on_position_opened/changed/closed`. Подписки: `subscribe_bars(bar_type)`, `subscribe_quote_ticks(id)`,
`subscribe_trade_ticks(id)`, `subscribe_order_book_deltas(id, depth=...)`, `subscribe_funding_rates`; запросы `request_bars/quote_ticks/trade_ticks`. Ордера:
`self.order_factory.market/limit/stop_market/stop_limit/market_if_touched/limit_if_touched/trailing_stop_market/trailing_stop_limit/market_to_limit(...)`,
`submit_order(order)`, `submit_order_list`, `modify_order`, `cancel_order/cancel_all_orders(instrument_id)`, `close_position/close_all_positions(instrument_id)`;
состояние `self.cache.instrument(id)`, `self.cache.bar_count(bar_type)`, `self.portfolio.is_flat/is_net_long/net_position(id)`, `self.portfolio.account(venue)`; индикаторы
`register_indicator_for_bars(bar_type, ema)`. Примеры в пакете: `nautilus_trader/examples/strategies/{ema_cross,ema_cross_bracket,volatility_market_maker,market_maker,orderbook_imbalance,...}.py`.

**Скелет live-node: Bybit data + Sandbox exec (API 1.221.0; файл `nautilus_sandbox_probe.py`, сокращённо):**
```python
import asyncio, time
from decimal import Decimal
from nautilus_trader.adapters.bybit import BybitDataClientConfig, BybitLiveDataClientFactory, BybitProductType
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.factory import SandboxLiveExecClientFactory
from nautilus_trader.config import InstrumentProviderConfig, LoggingConfig, TradingNodeConfig, StrategyConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.currencies import BTC, USDT
from nautilus_trader.model.data import BarType, Bar, QuoteTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.trading.strategy import Strategy

IID = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")
BAR = BarType.from_str("BTCUSDT-LINEAR.BYBIT-5-MINUTE-LAST-EXTERNAL")   # kline.5.BTCUSDT, только confirm==true

class MyConfig(StrategyConfig, frozen=True): ...
class MyStrategy(Strategy):
    def on_start(self):
        self.subscribe_quote_ticks(IID)      # orderbook.1 -> L1 для sandbox
        self.subscribe_bars(BAR)
    def on_bar(self, bar: Bar):
        if self.portfolio.is_flat(IID):
            self.submit_order(self.order_factory.market(IID, OrderSide.BUY, Quantity.from_str("0.010")))
    def on_order_filled(self, e): self.log.info(f"fill {e.last_qty}@{e.last_px} fee={e.commission}")

def make_instrument():  # вариант A: ручной инструмент (без REST/ключей); вариант B: load_all=True + реальные ключи
    now = int(time.time()*1e9)
    return CryptoPerpetual(IID, Symbol("BTCUSDT"), BTC, USDT, USDT, False, 1, 3, Price.from_str("0.1"), Quantity.from_str("0.001"), now, now,
                           min_quantity=Quantity.from_str("0.001"), margin_init=Decimal("0.01"), margin_maint=Decimal("0.005"),
                           maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.00055"))

async def main():
    cfg = TradingNodeConfig(
        trader_id=TraderId("LAB-001"),
        logging=LoggingConfig(log_level="INFO"),
        data_clients={"BYBIT": BybitDataClientConfig(
            api_key="dummy", api_secret="dummy",                      # при load_all=True нужны реальные ключи (подписанные REST)
            product_types=[BybitProductType.LINEAR],
            instrument_provider=InstrumentProviderConfig(load_all=False),
            update_instruments_interval_mins=None)},
        exec_clients={"BYBIT": SandboxExecutionClientConfig(
            venue="BYBIT", starting_balances=["10_000 USDT"], base_currency="USDT",
            account_type="MARGIN", oms_type="NETTING", default_leverage=Decimal(5),
            book_type="L1_MBP", bar_execution=True, trade_execution=False)},
        timeout_connection=30.0, timeout_reconciliation=5.0, timeout_portfolio=5.0, timeout_disconnection=5.0, timeout_post_stop=2.0)
    node = TradingNode(config=cfg)
    node.trader.add_strategy(MyStrategy(MyConfig()))
    node.add_data_client_factory("BYBIT", BybitLiveDataClientFactory)
    node.add_exec_client_factory("BYBIT", SandboxLiveExecClientFactory)
    node.build()
    node.kernel.cache.add_instrument(make_instrument())              # вариант A
    try:    await node.run_async()
    finally: await node.stop_async(); node.dispose()
asyncio.run(main())
```
Несколько стратегий: `node.trader.add_strategy(...)` для каждой (общий кошелёк sandbox; для изоляции — отдельные процессы/venue-алиасы).

**Оценка сложности.** Высокая: строгая типизация (`Price`, `Quantity`, `InstrumentId`), event-driven модель, Cython/Rust-ядро, конфиги `msgspec`. Порт «pandas-стратегии»
требует переписать логику под события бара и индикаторы Nautilus (или считать pandas внутри `on_bar` по кешу баров). Зато самая честная симуляция для
tick/orderbook-стратегий и единый код для бэктеста/сэндбокса/live.

---

## 4. Лёгкий собственный движок: pybit + ccxt

**pybit 5.17.0** (`pybit.unified_trading`): `HTTP(testnet=False, demo=False, api_key=None, api_secret=None)` — `get_kline(category="linear", symbol="BTCUSDT", interval="5", limit=200)`,
`get_instruments_info`, `get_orderbook`, `get_tickers` (публичные, без ключей); `WebSocket(channel_type="linear"|"spot"|"inverse"|"option"|"private", testnet=False, demo=False,
ping_interval=20, ...)` со стримами `kline_stream(interval, symbol|list, callback)`, `orderbook_stream(depth, symbol, cb)`, `trade_stream`, `ticker_stream`, `liquidation_stream`,
`all_liquidation_stream`, private: `order_stream/position_stream/execution_stream/wallet_stream`. URL: `wss://stream.bybit.com/v5/public/<channel>`
(testnet `stream-testnet`, demo `stream-demo`). Реализация на `websocket-client` (потоки, колбэки), не asyncio. Проверено live (`ws_probe2.py`).

**ccxt 4.5.84** (`ccxt.bybit`, v5): `has`: fetchOHLCV, fetchMarkets, fetchOrderBook, fetchTrades, fetchFundingRateHistory, fetchLeverageTiers, setLeverage, setMarginMode,
setPositionMode, fetchPositions, sandbox (testnet: `api-testnet.bybit.com`), `options.enableDemoTrading` для demo. Таймфреймы `1m,3m,5m,15m,30m,1h,2h,4h,6h,12h,1d,1w,1M`;
`options.defaultType="swap"`, `defaultSubType="linear"`; символы `BTC/USDT:USDT` (perp) / `BTC/USDT` (spot). **ccxt.pro** (`ccxt.pro.bybit`): `watchOHLCV`, `watchOrderBook`,
`watchTicker`, `watchTrades`, `watchOrders/Positions/Balance`; WS-URL те же v5. Проверено live с обходом REST через `set_markets` (`ws_probe3.py`): три апдейта 1m-свечи, стакан, тикер.

**Схема мини-движка.** WS `kline.{tf}` → буфер свечей (последний бар мутирует до `confirm=true`) → стратегия на pandas → симулятор исполнения: market по `orderbook.1`/`tickers`
bid1/ask1 (+ фикс. проскальзывание, taker 0.055% / maker 0.02% для linear по умолчанию, уточнить по счёту), limit — по касанию best-цены или по `publicTrade`; funding —
по `fundingRate/nextFundingTime` из тикера; прогрев — `HTTP.get_kline` или `ccxt.fetch_ohlcv`. Всё это ~300–500 строк, но без бэктеста/UI/учёта, которые у Freqtrade уже есть.

---

## 5. Рекомендуемое соответствие «тип стратегии → фреймворк»

| Тип стратегии (что обычно лежит на GitHub) | Фреймворк | Почему |
|---|---|---|
| Индикаторные/сигнальные на свечах (RSI/MACD/EMA-cross, Supertrend, breakout, «TradingView-порты»), long/short, 1m–1d | **Freqtrade** | pandas-интерфейс 1:1, `can_short`, dry-run без ключей, FreqUI, бэктест/hyperopt на тех же данных, 1 контейнер = 1 стратегия |
| Multi-timeframe / с BTC-корреляцией / DCA / трейлинг-стопы | **Freqtrade** | `@informative`, `adjust_trade_position`, `custom_stoploss`, `custom_exit` |
| ML/FreqAI-подобные на фичах из свечей | Freqtrade (FreqAI) | встроенный пайплайн |
| Тиковые/orderbook (imbalance, микроструктура), стратегии с точным intrabar-исполнением стопов, простые MM | **Nautilus** (venv py3.12+) | quotes/trades/deltas в реальном времени, честный `SimulatedExchange`, stop/trailing-ордера, единый код для backtest/sandbox/live |
| Grid, PMM/Avellaneda, XEMM, stat-arb на **spot** | Hummingbot (paper `bybit_paper_trade`) или Nautilus | готовые контроллеры/executors; для перпов в paper — только Nautilus |
| Перп-MM / funding-arb в paper-режиме | Nautilus sandbox (или Freqtrade, если сводится к сигналам) | Hummingbot perp paper не работает |
| Быстрые прототипы «одна идея — один скрипт», нестандартная логика исполнения | pybit/ccxt.pro + свой мини-движок | минимум зависимостей, WS-kline даёт свечи без REST |

---

## 6. Gotchas (сводно)

1. **Nautilus + Python 3.11 = 1.221.0 навсегда.** Все релизы с 2026-01 требуют 3.12+. Держите Nautilus в отдельном venv 3.12/3.13.
2. **Nautilus Bybit требует API-ключи даже для данных** (подписанные `coin/query-info`, `account/fee-rate` при загрузке инструментов; фабрика читает `BYBIT_API_KEY`).
   Обход: `load_all=False` + ручной `CryptoPerpetual` в кеш (комиссии/margin задаёте сами) — проверено, что узел строится и sandbox коннектится без REST.
3. Nautilus sandbox: `FillModel`/`LatencyModel`/`FeeModel` захардкожены; при `L1_MBP` market-ордер не учитывает глубину; бары нужны с `ts_init` на закрытии (`bars_timestamp_on_close=True`).
4. **Freqtrade dry-run всё равно ходит в REST Bybit** (`load_markets`, leverage tiers, стакан для каждого dry-ордера, funding history) — на VPS это нормально, в гео-блоке нет.
5. Freqtrade: значения `stoploss`/`minimal_roi`/`timeframe` и др. из config.json **перекрывают** стратегию; `jwt_secret_key` ≥ 32 символов; один процесс = одна стратегия;
   Bybit только isolated, one-way, без inverse; UTA → «один субаккаунт на бота» (для dry-run неважно); `demo_trading` несовместим с `dry_run`.
6. Freqtrade dry-run: лимитка, пересекающая спред >1%, становится market; market — интерполяция по стакану с cap 5%; частичных заполнений нет; комиссия из ccxt-markets или `fee`.
7. Hummingbot: conda-only, тяжёлый; один бот на контейнер; **paper-trade — spot-семантика, перпы в paper не работают** (executors ломаются на `position_mode`); perp-коннектор рассчитан на UTA.
8. Bybit WS: linear `tickers` — snapshot + delta (мержить состояние); spot `tickers` без bid/ask (нужен `orderbook.1`); `kline` — мутирующая свеча до `confirm=true`.
9. Все REST-хосты Bybit (`api`, `api-demo`, `api-testnet`) в этом контейнере недоступны — конфиги/сниппеты проверены до границы REST.

---

## 7. Точные версии (проверено, `uv venv --python 3.11`, Python 3.11.15, uv 0.8.17)

```
freqtrade==2026.8        freqtrade-client==2026.8   ccxt==4.5.84        ta-lib==0.7.1      ft-pandas-ta==0.3.16   technical==1.7.0
nautilus-trader==1.221.0 (последняя для py3.11; PyPI latest 1.231.0 и 2.0.0rc5 требуют >=3.12)   msgspec==0.21.1   uvloop==0.22.1
pybit==5.17.0            websocket-client==1.9.2    websockets==17.1    aiohttp==3.14.3
pandas==2.3.3            numpy==2.4.6               pyarrow==25.0.1     sqlalchemy==2.1.1  fastapi==0.141.1       uvicorn==0.54.0
```
Исходники: freqtrade master `c0a991f5` (2026-09-24, `2026.9-dev`); hummingbot master (VERSION 2.17.0); nautilus master `a0bd798c` (2026-09-26, v2.0.0rc6) и `develop_v1` (1.231.0).
Hummingbot в контейнере не устанавливался (conda + Cython-сборка; выводы по исходникам).

Файлы проверки: `ws_probe.py/.out` (websockets, linear/spot/inverse), `ws_probe2.py/.out` (pybit WS), `ws_probe3.py/.out` (ccxt.pro без REST),
`nautilus_sandbox_probe.py/.out` (TradingNode + Sandbox без REST), `ft_user_data/config_bybit_futures_dryrun.json` (+ `strategies/SampleStrategy.py`, провалидировано).
