# queue_mm — мейкерский сбор спреда с учётом очереди

**Ветка:** `strategy/queue-mm` · **Движок:** hope (собственный asyncio-движок с очередной моделью исполнения) ·
**Рынок:** Bybit linear USDT-перпетуалы · **Тип:** market making / захват спреда, тиковый уровень, обе стороны.

## Источник

Идея: hftbacktest, ноутбук [«Queue-Based Market Making in Large Tick Size Assets»](https://github.com/nkaz001/hftbacktest/blob/master/examples/Queue-Based%20Market%20Making%20in%20Large%20Tick%20Size%20Assets.ipynb)
(nkaz001/hftbacktest, MIT). В активах с большим шагом цены спред почти всегда равен одному тику, а тик
составляет десятки базисных пунктов — заработок за круг (1 тик) заметно больше комиссий мейкера (2 × 0.02 %).
Всё решает место в очереди на лучшей цене и adverse selection (исполнение прямо перед движением против нас).

Почему именно наш движок, а не hftbacktest/Nautilus/Hummingbot: hftbacktest — бэктестер (live только на Rust с ключами
и без paper-режима); Nautilus SandboxExecutionClient и paper-режим Hummingbot исполняют лимитки «по касанию» без очереди,
что для мейкерской стратегии даёт заведомо завышенный результат (см. `research/frameworks.md` в сессии). Paper-брокер
hope моделирует латентность, очередь впереди ордера, исполнение по публичным сделкам и снятие при истощении уровня.

## Логика (strategy.py)

1. **Активный набор символов** (пересчёт каждые `refresh_secs`): оборот 24 ч ≥ `min_turnover_24h_usd`, медиана спреда
   за 120 с ≥ `min_spread_med_bps`, сделок/мин ≥ `min_trades_per_min`, спред/волатильность ≥ `min_spread_vol_ratio`;
   score = spread_med · √(сделок/мин) / (1 + vol); котируются top-`max_active_symbols`.
2. **Резервационная цена** = микроцена (book pressure по объёмам на лучших уровнях) − `skew_ticks`·тик·(позиция/лимит).
   Котировки: bid = ⌊reservation − 0.49·тик⌋, ask = ⌈reservation + 0.49·тик⌉, но не лучше лучших цен (`quote_mode: join`)
   либо на тик внутрь, если спред это позволяет (`improve`).
3. **Условия входа:** текущий спред ≥ `min_spread_bps`; минутная волатильность ≤ `max_vol_bps`; дисбаланс тейкерского
   потока за `toxicity_window_secs` не против нашей стороны (|imb| < `toxicity_imbalance`); ожидаемое время исполнения
   (очередь впереди / оборот в нашу сторону) ≤ `max_expected_fill_secs`.
4. **Очередная модель:** если объём на нашем уровне упал ниже `depletion_cancel_ratio` от объёма при постановке, а мы
   ещё не впереди очереди — ордер снимается (уровень вот-вот пробьют).
5. **Инвентарь:** при достижении `max_position_notional_usd` котируется только сторона выхода; позиция старше
   `max_hold_secs` выходит пассивно на тик внутрь, ещё через `stale_taker_secs` — тейкером.
6. Ордера старше `max_order_age_ms` переставляются; перестановка не чаще `min_requote_ms`.

Все параметры — в `config.yaml` (`strategy.params`), значения по умолчанию — в `QueueMM.default_params()`.

## Запуск

```bash
# live paper (публичный WebSocket Bybit, ключи не нужны)
hope run -c strategies/queue_mm/config.yaml
# то же в Docker: STRATEGY_CONFIG=strategies/queue_mm/config.yaml docker compose up -d --build  → http://localhost:8000

# бэктест-фильтр на архиве сделок (BBO-прокси по сделкам, очередь = --queue-usd)
hope backtest -c strategies/queue_mm/config.yaml -s GRAMUSDT,ATOMUSDT,RENDERUSDT,XLMUSDT,ADAUSDT,NEARUSDT \
  --from 2026-09-23 --to 2026-09-25 --db data/bt_queue_mm.db

# честный реплей: записать live-поток (BBO с объёмами + сделки) и прогнать по нему любые параметры
hope run -c strategies/queue_mm/config.yaml --set store.record_market=true --duration 3600
hope replay -c strategies/queue_mm/config.yaml --source-run <run_id> --set strategy.params.min_spread_bps=8
```

Список кандидатов в `config.yaml` получен сканером `hope scan` (оборот ≥ 4 M USDT, шаг цены ≥ 1 б.п.); стратегия
сама сужает его до активного набора. Обновляйте список раз в несколько дней: `hope scan --min-turnover 4000000 --out scan.csv`.

## Результаты

### Бэктест-фильтр (архив сделок, 2026-09-23 … 25, 6 символов, параметры по умолчанию)

| Показатель | Значение |
|---|---|
| Исполнений / кругов | 31 / 15 |
| Чистый PnL | −1.31 USDT (комиссии 0.67, фандинг +0.09) |
| Доля мейкерских исполнений | 97 % |
| Win rate / profit factor | 6.7 % / 0.02 |
| Markout +5 с | −0.22 б.п. |
| Сделок в день | 5 |

Вывод фильтра: в таком виде стратегия **не проходит** — сделок слишком мало (спред ≥ 6 б.п. на ликвидных перпетуалах
Bybit встречается редко), а те, что есть, исполняются перед движением против нас. Бэктест на сделках занижает
частоту исполнений (нет объёмов на уровнях, очередь оценивается константой), поэтому решающим остаётся живой тест.

### Живой paper-прогон (4 мин, 60 символов, контейнер разработки)

10 исполнений, активный набор 5–10 символов (BIO, CAKE, GRAM, LONGXIA, PENDLE, STONK, WAVES …), PnL −0.67 USDT.
Стенд работает (реконнектов нет, 84 k событий), для выводов нужны сутки и больше.

### Статус

`⏳ в тестировании` — нужен живой прогон ≥ 3 суток с записью потока (`store.record_market=true`), затем подбор
`min_spread_bps`, `depletion_cancel_ratio`, `max_expected_fill_secs` по реплею.

## Что можно улучшить (по порядку ожидаемого эффекта)

1. Использовать `orderbook.50` и котировать на втором уровне, когда лучший уровень тонкий (меньше adverse selection).
2. Лид-лаг Binance→Bybit как альфа (beatzxbt/smm): недоступно из этого контейнера (Binance гео-блокирован), на VPS —
   `wss://fstream.binance.com` бесплатно.
3. Требовать спред ≥ 2 тика в среднетиковых активах вместо одного «крупного» тика.
