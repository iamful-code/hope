# hope — лаборатория торговых стратегий для Bybit

Платформа для перебора публичных стратегий: каждая стратегия запускается в **paper-торговле в реальном
времени** на публичном потоке Bybit, перед этим проходит **бэктест-фильтр** на архиве сделок Bybit, а
результаты в реальном времени видны в **мониторе**. Все движки пишут в одну схему SQLite, поэтому
стратегии на разных фреймворках сравниваются в одном мониторе.

```
                 ┌──────────────── ветка strategy/<имя> ────────────────┐
                 │ strategies/<имя>/strategy.py  config.yaml  README.md │
                 └───────────────────────┬──────────────────────────────┘
                                         ▼
 Bybit public WS ──► hope.engine.live ──► paper-брокер ──► портфель/риск ──► SQLite (data/hope.db)
 (стакан, сделки,     (или backtest:      (латентность,                          │
  kline, tickers)      архив сделок)       очередь, post-only,                   ▼
                                           комиссии, фандинг)       hope.monitor (FastAPI + Plotly)
```

## Что внутри

| Модуль | Назначение |
|---|---|
| `hope.bybit.ws` | Публичный WebSocket V5: несколько соединений, батч-подписки, ping, реконнект; топики `orderbook.{d}`, `publicTrade`, `kline`, `tickers` |
| `hope.bybit.rest` / `instruments` | REST (инструменты, тикеры, свечи) с fallback на кэш `data/instruments_*.json` и вывод шага цены/лота из потока (REST Bybit гео-блокируется в ряде стран) |
| `hope.bybit.history` | Архив сделок `public.bybit.com` (по дням, без ключей и гео-ограничений) → Parquet; свечи любого таймфрейма |
| `hope.bybit.scan` | Сканер символов по WebSocket: оборот, шаг цены в б.п., спред, фандинг (`hope scan`) |
| `hope.paper` | Paper-брокер (модель исполнения из `spr/core/src/paper.rs`), портфель по средней цене, риск-лимиты и kill-switch |
| `hope.engine` | `Strategy` API + `Context`; `LiveEngine` (WS) и `BacktestEngine` (архив) на одном ядре; markout-аналитика исполнений |
| `hope.store` | SQLite (WAL), отдельный поток-писатель; схема `runs/orders/fills/equity/positions/symbol_stats/metrics/events/markouts` |
| `hope.monitor` / `hope.analytics` | Веб-монитор (только чтение БД): equity, просадка, круги (FIFO), markout, PnL по символам/часам/тегам, сравнение запусков |
| `strategies/` | Тестируемые стратегии (каждая — своя ветка `strategy/<имя>`) |

### Модель бумажного исполнения

* Ордер попадает на «биржу» через `paper.latency_ms`; отмена тоже действует с задержкой (в промежутке ордер может исполниться).
* Лимитный post-only ордер, пересекающий рынок при активации, отклоняется (`rejected_post_only`); без post-only — исполняется как тейкер.
* При активации очередь впереди = показанный объём на нашей цене (0, если мы внутри спреда); если объём на уровне
  уменьшился ниже нашей оценки — очередь сокращается (отмены впереди нас).
* Публичная сделка на нашей цене с нужной стороной тейкера сначала съедает очередь впереди, остаток исполняет нас;
  сделка сквозь нашу цену или сдвиг BBO сквозь неё исполняют остаток.
* Тейкерные ордера исполняются по касанию противоположной стороны плюс `taker_slippage_bps`.
* Комиссии Bybit VIP0 по умолчанию: мейкер 0.02 %, тейкер 0.055 %. Фандинг начисляется по ставке из тикера в момент `nextFundingTime`.

В бэктесте стакана нет: BBO оценивается по сделкам (тейкерская покупка по p ⇒ ask = p, продажа ⇒ bid = p), очередь
впереди задаётся `--queue-usd`, фандинг — постоянной ставкой `--funding`. Это консервативно для тейкерских стратегий
и приблизительно для мейкерских; итоговый критерий — живой paper-тест.

## Установка

```bash
uv venv .venv && uv pip install -p .venv/bin/python -e ".[dev]"     # или: pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Docker (движок + монитор, конфиг стратегии из `strategies/current/config.yaml`):

```bash
cp .env.example .env            # STRATEGY_CONFIG, MONITOR_PORT
docker compose up -d --build    # монитор: http://localhost:8000
docker compose logs -f engine
```

> Bybit блокирует запросы из США и ряда других стран (CloudFront 403). Публичный WebSocket при этом обычно
> доступен, поэтому движок работает без REST: метаданные инструментов берутся из кэша или выводятся из потока.
> Для точных шагов цены/лота выполните `hope instruments` с машины, где REST доступен, — кэш попадёт в `data/`.

## Команды

```bash
hope run -c strategies/<имя>/config.yaml [--duration N] [--name ...] [--set section.key=value]
hope backtest -c strategies/<имя>/config.yaml --from 2026-09-01 --to 2026-09-07 [--db data/bt.db] [--queue-usd 20000]
hope download -s BTCUSDT,ETHUSDT --from 2026-09-01 --to 2026-09-07
hope scan --duration 30 --min-turnover 5000000 [--min-tick-bps 5] [--out scan.csv]
hope monitor --db data/hope.db --port 8000
hope instruments            # обновить кэш инструментов через REST
```

Конфигурация: `config/base.yaml` → `strategies/<имя>/config.yaml` → переменные `HOPE__section__key` → `--set`.

## Как написать стратегию

```python
from hope.engine.strategy import Strategy, Context
from hope.types import Side, Purpose

class MyStrategy(Strategy):
    name = "my"
    @classmethod
    def default_params(cls): return {"n": 20, "notional_usd": 100.0}

    def on_candle(self, ctx: Context, symbol: str, candle):
        closes = ctx.candles(symbol).close             # numpy-массив закрытых свечей
        if len(closes) < self.p("n"): return
        if closes[-1] > closes[-self.p("n"):].mean() and ctx.position(symbol).qty == 0:
            ctx.place_market(symbol, Side.BUY, ctx.qty_for_notional(symbol, self.p("notional_usd")))

    def on_bbo(self, ctx, symbol, bbo): ...           # каждое изменение лучших цен
    def on_trade(self, ctx, symbol, trade): ...       # каждая публичная сделка
    def on_timer(self, ctx): ...                      # раз в strategy.timer_ms
    def on_fill(self, ctx, fill): ...
```

`Context`: `bbo(sym)`, `book(sym)`, `candles(sym)`, `stats(sym)` (медиана спреда, волатильность, сделки/мин,
дисбаланс потока), `position(sym)`, `orders()`, `place_limit(...)`, `place_market(...)`, `cancel(...)`,
`close_position(sym)`, `qty_for_notional(...)`, `metric(name, value, sym)` (попадает в монитор), `event(msg)`.
Один и тот же класс работает в live и в бэктесте.

## Ветки

* `claude/keen-knuth-rvy3au` — платформа (эта ветка).
* `strategy/<имя>` — одна тестируемая стратегия: движок/адаптер, конфиг, README с логикой, источником,
  результатами бэктест-фильтра и критериями «рабочести».

## Критерии «рабочей» стратегии (по умолчанию)

За период живого paper-теста: чистый PnL после комиссий и фандинга > 0; ≥ 100 кругов; profit factor ≥ 1.2;
доля комиссий в валовом результате < 50 %; средний markout +5 с не отрицательный (нет систематического adverse
selection); просадка < 10 % от капитала. Панель «Вердикт» в мониторе показывает эти проверки.
