# Каталог публичных стратегий для лаборатории hope (Bybit, paper trading)

Дата исследования: 2026-09-26. Источники: GitHub (все репозитории клонированы `--depth 1`, код прочитан, правила входа/выхода цитируются из исходников, а не из README), поиск по awesome-спискам и блогам. Звёзды взяты со страниц репозиториев на дату исследования (приближённо).

## 0. Контекст комиссий (главный фильтр)

| Рынок Bybit VIP0 | Maker | Taker | Round-trip maker/maker | Round-trip taker/taker |
|---|---|---|---|---|
| Linear USDT perp | 0.020% | 0.055% | **0.040%** | **0.110%** |
| Spot | 0.10% | 0.10% | 0.20% | 0.20% |

Следствия для отбора:
- Любая taker-стратегия на 1m/5m обязана в среднем зарабатывать заметно больше 0.11% за сделку. Для BTC на 1m средний диапазон свечи ~0.05–0.1% — почти все «скальперы по индикаторам» на taker-ордерах математически убыточны.
- Maker-стратегии (MM, грид, лимитные mean-reversion) платят 0.04% за цикл — единственный класс, где «много сделок» совместимо с комиссиями VIP0. Но ребейта у VIP0 нет, а большинство публичных HFT-примеров (hftbacktest) калиброваны под ребейт −0.005%…−0.0025%.
- Spot 0.2% за цикл делает спот пригодным только для funding/basis-конструкций, не для скальпинга.

Обозначения: **Freq** — оценка сделок/день на один символ; **Порт** — трудоёмкость запуска на Bybit (low/med/high); **Paper** — как получить paper-режим.

---

## 1. Полный каталог

### (a) Freqtrade-стратегии

Freqtrade поддерживает Bybit spot и USDT-perp (futures, `trading_mode: futures`, `margin_mode: isolated`) нативно, `dry_run: true` = полноценный paper trading на живых данных. Для шортов стратегия должна иметь `can_short = True`.

#### A1. `Scalp` — freqtrade/freqtrade-strategies
- URL: https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/berlinguyinca/Scalp.py
- ★5.5k (весь репозиторий), GPL-3.0, последний коммит репо 2026-09-08 (сама стратегия 2018 г.)
- Логика (из кода): `timeframe='1m'`, вход: `open < EMA5(low) & ADX > 30 & fastk < 30 & fastd < 30 & crossed_above(fastk, fastd)`; выход: `open >= EMA5(high) | crossed_above(fastk,70) | crossed_above(fastd,70)`; `minimal_roi={"0":0.01}`, `stoploss=-0.04`. Автор: «нужно ≥60 параллельных сделок, чтобы покрыть неизбежные убытки».
- Тип: intraday scalping по осцилляторам; 1m; spot/perp (long only, шортов нет); Freq: 5–20.
- Фреймворк: Freqtrade; Порт: low (ничего менять не нужно).
- Оценка: цель 1% > комиссий, но у стох-кросса на 1m нет предсказательной силы; exit-сигналы часто срабатывают в минус; SL −4% против TP +1% — отрицательная асимметрия. Годится как **негативный контроль** для стенда.

#### A2. `ReinforcedSmoothScalp` — freqtrade/freqtrade-strategies
- URL: https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/berlinguyinca/ReinforcedSmoothScalp.py
- Те же метаданные репо.
- Логика: 1m + ресемпл 5m SMA как тренд-фильтр (`resample_sma < close`); вход: `mfi < buy_mfi & fastd < x & fastk < x & adx > x & crossed_above(fastk,fastd)`; выход: `open > ema_high & (mfi > / fastd > / fastk > / adx < / cci >)`; ROI 2%, SL −10%.
- 1m; long only; Freq: 3–10. Freqtrade; Порт: low.
- Оценка: то же, что A1, но SL −10% ещё хуже по асимметрии. Хайпероптимизируемые пороги = переобучение.

#### A3. `BinHV45` — freqtrade/freqtrade-strategies
- URL: https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/berlinguyinca/BinHV45.py
- Логика: 1m, BB(40,2); вход: `bbdelta > close*0.007 & closedelta > close*0.017 & tail < bbdelta*0.25 & close < lower.shift() & close <= close.shift()` — ловля резкого (≥1.7% за минуту) пробоя нижней полосы с маленькой тенью; выход только по ROI 1.25% / SL −5%.
- Тип: event-driven mean reversion после «панической» свечи; 1m; long only (но условие зеркалится для шортов в 10 строк); Freq: 0–3 (всплески в волатильные дни).
- Freqtrade; Порт: low (зеркало для `enter_short` — 30 минут работы).
- Оценка: единственная 1m-стратегия из репо с содержательной идеей (реверсия после ликвидационных проколов в альтах). Минусы: редкие сигналы на BTC/ETH, SL −5% vs TP 1.25%. Стоит тестировать на корзине волатильных альтов с шорт-зеркалом.

#### A4. `ClucMay72018` / `CombinedBinHAndCluc` (семейство Cluc → ClucHAnix)
- URL: https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/berlinguyinca/CombinedBinHAndCluc.py ; наследник `ClucHAnix_5m.py` в https://github.com/ssssi/freqtrade_strs/tree/master/ClucHAnix
- Логика Cluc: 5m; вход: `close < EMA50 & close < 0.985*BB20_lower & volume < 20*mean(30)`; выход: `close > BB20_mid`; ROI 1% (Combined: 5%), SL −5%. ClucHAnix: то же на Heikin-Ashi + `custom_stoploss`, ROCR-фильтр 1h, SL −0.99.
- Тип: mean reversion к середине BB; 5m; long only; Freq: 1–5.
- Freqtrade; Порт: low.
- Оценка: покупка «проколов» ниже BB с выходом на средней — честная реверсия, комиссии 0.11% при цели ~1% терпимы. Слабость: в трендах вниз серия входов до SL; работает только в боковике/бычьем рынке.

#### A5. `Strategy005` — freqtrade/freqtrade-strategies
- URL: https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/Strategy005.py
- Логика: 5m; вход: `volume > 4*mean(150) & close < SMA40 & fastd > fastk & rsi > 26 & fastd > 1 & fisher_rsi_norma < 5`; выход: `crossed_above(rsi,74) & macd < 0 & minus_di > 4` или `sar > close & fisher_rsi > 30`; ROI ступенчато 4%→1%; SL −10%.
- Тип: объёмный всплеск + перепроданность; 5m; long only; Freq: 0.5–2.
- Freqtrade; Порт: low. Оценка: низкая частота, SL −10%; в каталоге как представитель «классики 2018», в приоритеты не входит.

#### A6. `FReinforcedStrategy` (папка futures) — freqtrade/freqtrade-strategies
- URL: https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/futures/FReinforcedStrategy.py
- Логика: 5m, `can_short = True`; тренд-фильтр по ресемплу 60m SMA (`close > resample_sma` для лонга, `<` для шорта) + `crossed_above/below(EMA_short, EMA_long)` (периоды — hyperopt-параметры); выход: `ADX < pos_entry_adx`; ROI {"0":5%, "30":10%, "60":7.5%}; SL −5%.
- Тип: тренд-следование EMA-кросс; 5m; long/short; Freq: 0.5–2.
- Freqtrade; Порт: low (готовая futures-стратегия).
- Оценка: цели крупные, комиссии не проблема, но EMA-кросс на 5m = пила; ожидаемый винрейт <40%. Полезна как эталонная «futures + short» стратегия для проверки, что стенд корректно исполняет шорты.

#### A7. `TrendFollowingStrategy` (папка futures) — freqtrade/freqtrade-strategies
- URL: https://github.com/freqtrade/freqtrade-strategies/blob/main/user_data/strategies/futures/TrendFollowingStrategy.py
- Логика: 5m; `enter_long: close > EMA20 & close.shift(1) <= EMA20.shift(1) & OBV растёт`; `enter_short` — зеркально; выходы — обратный кросс; ROI 15/10/5%; SL −26.5%; трейлинг 5% после 10%.
- Тип: EMA20-кросс + OBV; 5m; long/short; Freq: 2–6.
- Freqtrade; Порт: low. Оценка: чистый шум на 5m, каждое переворачивание −0.11%; SL −26.5% недопустим. Только как smoke-test.

#### A8. NostalgiaForInfinity X8 — iterativv/NostalgiaForInfinity
- URL: https://github.com/iterativv/NostalgiaForInfinity (файл `NostalgiaForInfinityX8.py`, 53 809 строк)
- ★3.4k, GPL-3.0, последний коммит 2026-09-26 (ежедневная активность)
- Логика (из кода): `timeframe="5m"`, `info_timeframes=["15m","1h","4h","1d"]`, `startup_candle_count=800`; 85 long-условий входа и 32 short-условия; `can_short = True` включается в futures-режиме (`is_futures_mode`, `futures_mode_leverage=3.0`); `stoploss=-0.99`, выходы через `custom_exit` (десятки правил по профиту/индикаторам); `position_adjustment_enable=True` — rebuy/grind/rapid-режимы (DCA). Есть готовые `configs/pairlist-static-bybit-futures-usdt.json`, `blacklist-bybit.json`.
- Тип: многоусловный dip-buying + DCA; 5m; spot & perp, long/short; Freq: 0.1–1 на пару (5–30/день на 20–40 парах).
- Freqtrade; Порт: low (нативные Bybit-конфиги), но тяжёлая: 800 стартовых свечей ×5 таймфреймов на 30+ пар.
- Оценка: самая обкатанная community-стратегия, живёт с 2021 г.; результаты сильно зависят от режима рынка, grind/DCA сглаживает эквити, но прячет риск. Для нашей задачи минус — низкая частота на символ. Обязательна как «индустриальный эталон» Freqtrade-ветки.

#### A9. `BB_RPB_TSL` — jilv220/BB_RPB_TSL
- URL: https://github.com/jilv220/BB_RPB_TSL
- ★214, GPL-3.0, последний коммит 2022-02-20 (заброшена)
- Логика: 5m + 1h informative; ~20 семейств входов (`is_dip`, `is_break`, `is_local_uptrend`, `is_ewo`, `is_cofi`, `is_gumbo`, `is_nfix_39` …), `use_custom_stoploss=True`, `custom_sell` (trailing по индикаторам), `stoploss=-0.99`.
- Тип: dip-buying сборная солянка из NFI/SMAOffset; 5m; spot long only; Freq: 0.3–1.
- Freqtrade; Порт: med (старый API `populate_buy_trend`/`custom_sell` → нужна миграция на `populate_entry_trend`/`custom_exit`).
- Оценка: переобучена под 2021 г., заброшена, при бэктестах на 2024–2026 обычно проигрывает NFI. В каталоге для полноты, тестировать после A8/A11.

#### A10. `MultiMA_TSL` / `Cenderawasih` — stash86/MultiMA_TSL
- URL: https://github.com/stash86/MultiMA_TSL
- ★181, GPL-3.0, последний коммит 2026-04-19
- Логика: 5m; вход при `close < TRIMA/ZEMA/HMA * low_offset` (три включаемых условия) и `EWO` в диапазонах (`ewo_low=-19.6`, `ewo_high=2.6`); выход `custom_sell` при `close > sellma*high_offset`, `custom_stoploss`, SL −25%.
- Тип: SMAOffset-семейство (покупка отката ниже смещённой MA); 5m; spot long only; Freq: 0.5–2.
- Freqtrade; Порт: low–med (проверить API). Автор: «не для live».
- Оценка: работает только на бычьем рынке; в падениях серия убытков; SL −25%.

#### A11. `E0V1EN` (+ `BinHV27_short`, `ClucHAnix_5m`) — ssssi/freqtrade_strs
- URL: https://github.com/ssssi/freqtrade_strs (файл `binance/dry_run/E0V1EN.py`, 146 строк)
- ★651 (самый популярный community-репо стратегий на 2026 г.), лицензия не указана, последний коммит 2026-07-25; автор ведёт публичный copy-trading на Binance (трек не верифицирован).
- Логика (из кода): 5m, `order_types` — все `market`; `buy_1: rsi_slow(20) падает & rsi_fast(4) < 40 & rsi(14) > buy_rsi & close < SMA15*k & cti(20) < 0.69 & фильтр 24h-изменения`; `buy_new: rsi_fast < 34 & rsi > 28 & close < SMA15*0.96 & cti < 0.69`; выход: `custom_exit` при `fastk > sell_fastx` в плюсе (для `buy_new`), `trailing_stop_positive=0.002` после offset 3%, ROI 100%, SL −25%, `protections`.
- Тип: краткосрочная перепроданность (RSI4) под SMA15 — mean reversion; 5m; long only (в репо есть `BinHV27_short.py` с `can_short=True`, 5m, SL −10%); Freq: 0.5–3.
- Freqtrade; Порт: low.
- Оценка: минималистичная и живая; market-ордера (taker 0.055%) приемлемы при целях 1–3%; главный риск SL −25% и long-only. Первая в очереди среди Freqtrade-кандидатов по соотношению простота/популярность.

### (b) Market making / грид / захват спреда

#### B1. Hummingbot `pure_market_making` (v1) — hummingbot/hummingbot
- URL: https://github.com/hummingbot/hummingbot/tree/master/hummingbot/strategy/pure_market_making
- ★20.2k, Apache-2.0, последний коммит 2026-09-22; есть коннекторы `bybit` (spot) и `bybit_perpetual`.
- Логика (config map): `bid_spread/ask_spread`, `order_refresh_time`, `order_levels`+`order_level_spread`, `inventory_skew_enabled` (+target base pct), `hanging_orders_enabled`, `order_optimization_enabled` (прыжок к лучшей цене), `ping_pong_enabled`, `filled_order_delay`, `max_order_age`, `minimum_spread`, `price_type` (mid/last/inventory_cost/custom).
- Тип: симметричные лимитные котировки вокруг mid; event-driven (тик 1с); spot/perp, обе стороны; Freq: 20–300 при спреде 0.05–0.1% и refresh 10–30с.
- Фреймворк: Hummingbot; Порт: none. Paper: `paper_trade` для спота, для `bybit_perpetual` — testnet-домен коннектора.
- Оценка: обязательный baseline. Плюс — maker 0.04% за цикл; минус — нулевая альфа, на BTC/ETH тесные котировки выбираются информированным потоком (adverse selection), в трендах копится инвентарь. На средних альтах с спредом ≥0.1% может быть около нуля.

#### B2. Hummingbot `avellaneda_market_making` (v1)
- URL: https://github.com/hummingbot/hummingbot/tree/master/hummingbot/strategy/avellaneda_market_making
- Параметры: `risk_factor` (γ), `order_amount_shape_factor` (η), `min_spread`, `order_refresh_time`, `order_levels`, `execution_timeframe_mode` (infinite / from-to / daily), `volatility_buffer_size`, `trading_intensity_buffer_size` (оценка κ по стакану). Resevation price и optimal spread по Avellaneda–Stoikov с оценкой σ и κ на лету.
- Тип: MM с контролем инвентаря; event-driven; spot/perp; Freq: как B1.
- Hummingbot; Порт: none.
- Оценка: теоретически обоснованный контроль инвентаря, но без альфы также проигрывает информированному потоку; чувствительна к γ и оценке κ. Тестировать парой с B1 на одинаковых символах для честного сравнения.

#### B3. Hummingbot `perpetual_market_making` (v1)
- URL: https://github.com/hummingbot/hummingbot/tree/master/hummingbot/strategy/perpetual_market_making
- Параметры: `bid_spread/ask_spread`, `order_refresh_time`, `long_profit_taking_spread`, `short_profit_taking_spread`, `stop_loss_spread`, `time_between_stop_loss_orders`. Логика: пока позиции нет — котирует обе стороны; после заполнения одной стороны ставит take-profit лимитку на `profit_taking_spread` и SL.
- Тип: MM для перпов с управлением позицией (по сути ping-pong); perp, long/short; Freq: 10–100.
- Hummingbot; Порт: none.
- Оценка: ближе всего к «правильному» MM на Bybit linear из v1-набора; SL на market-ордере съедает 0.055%. Хороший второй baseline после B1.

#### B4. Hummingbot V2 `pmm_simple` / `pmm_dynamic` (controllers/market_making)
- URL: https://github.com/hummingbot/hummingbot/blob/master/controllers/market_making/pmm_dynamic.py
- Логика: базовый конфиг `MarketMakingControllerConfigBase`: `buy_spreads/sell_spreads` (по умолчанию 1%,2%), `executor_refresh_time=300s`, `cooldown_time=15s`, тройной барьер на каждую позицию: `stop_loss=3%`, `take_profit=2%` (LIMIT), `time_limit=45min`, `leverage=20`, `position_mode=HEDGE`. `pmm_dynamic`: спреды в единицах NATR(14) на 3m-свечах, `reference_price = close*(1 + (0.5*z(MACD) + 0.5*sign(MACDh)) * NATR/2)` — сдвиг центра по моментуму.
- Тип: MM через PositionExecutor (каждый филл — позиция с TP-лимиткой); perp; Freq: с дефолтными спредами 1–2% — единицы/день; со спредами 0.1–0.2% — десятки.
- Hummingbot (V2); Порт: none (`connector_name: bybit_perpetual`).
- Оценка: архитектурно — грид с TP, а не классический MM. `pmm_dynamic` добавляет моментум-сдвиг котировок (плюс против adverse selection). Дефолты слишком широкие для «много сделок» — нужно ужать спреды и TP до 0.1–0.3%.

#### B5. Hummingbot V2 `grid_strike` / `multi_grid_strike` / `bollingrid`
- URL: https://github.com/hummingbot/hummingbot/blob/master/controllers/generic/grid_strike.py ; https://github.com/hummingbot/hummingbot/blob/master/controllers/directional_trading/bollingrid.py
- Логика: грид между `start_price`/`end_price` с `limit_price` (стоп всей сетки), `min_spread_between_orders=0.1%`, `max_open_orders=2`, `order_frequency=3s`, `take_profit=0.1%` ордером `LIMIT_MAKER` на каждый уровень; `side` LONG или SHORT (направленный грид). `bollingrid` строит грид динамически от BB(100, 2.0, 3m) с коэффициентами 0.25/0.75/0.35 и сигналом BBP < 0 (лонг-грид) / > 1 (шорт-грид).
- Тип: грид (maker/maker); perp & spot; Freq: при шаге 0.1% на волатильных альтах 50–500.
- Hummingbot; Порт: none.
- Оценка: цикл 0.1% минус 0.04% комиссий = +0.06% при возврате цены; классический риск грида — уход цены из диапазона (стоп по `limit_price` фиксирует убыток всей сетки). `bollingrid` частично решает выбор диапазона. Хороший кандидат для боковых альтов.

#### B6. Hummingbot V2 `pmm_mister` (generic)
- URL: https://github.com/hummingbot/hummingbot/blob/master/controllers/generic/pmm_mister.py
- Логика: спреды 0.05%, `take_profit=0.01%` LIMIT_MAKER, `executor_refresh_time=30s`, hanging executors, cooldown 60s, `target_base_pct=0.5` с min/max 0.3/0.7, `price_distance_tolerance` (не пересоздавать ордера при малом сдвиге), `leverage=20`.
- Тип: объёмогенерирующий MM (под ребейт/лидерборды); perp; Freq: очень высокая.
- Hummingbot; Порт: none.
- Оценка: TP 1 б.п. при комиссии 4 б.п. за цикл — на VIP0 гарантированный минус. Только если расширить TP ≥ 0.1%. Низкий приоритет.

#### B7. passivbot (`trailing_martingale`, `ema_anchor`) — enarjord/passivbot
- URL: https://github.com/enarjord/passivbot
- ★2.1k, Unlicense (public domain), последний коммит 2026-09-25, v8.1.0; Rust-ядро + Python; Bybit — первая в списке бирж.
- Логика (docs/config.bot.md, зеркало `passivbot-rust/src/entries.rs`): начальный вход `min(best_bid, EMA_low*(1-entry_initial_ema_dist))` объёмом `balance*WEL*initial_qty_pct`; довходы по `pos.price*(1-threshold_base_pct*mult(vol_1m, vol_1h, WE))` с `qty*double_down_factor`; закрытие `max(best_bid, pos.price*(1+close_threshold))` либо trailing-close (после хая ≥ порога и отката); auto-unstuck реализует малые убытки при просадке; Forager выбирает монеты по объёму/волатильности 1m. Все ордера лимитные (pure maker). Есть бэктестер и эволюционный оптимизатор, режим `fake_live` (симуляция live).
- Тип: контр-трендовый grid/DCA-мартингейл; 1m OHLCV + best bid/ask; perp, long и short (раздельно); Freq: 1–10 на монету, десятки на портфель из 10–20 монет.
- Фреймворк: собственный движок; Порт: none. Paper: `fake_live` или testnet.
- Оценка: самый зрелый Bybit-first открытый бот; комиссии не проблема (maker). Главный риск — мартингейл: отрицательная асимметрия, глубокие просадки в трендах, «unstuck» лишь размазывает убыток. Обязателен как эталон класса «грид/DCA».

#### B8. hftbacktest — High-Frequency Grid Trading (упрощённый GLFT) — nkaz001/hftbacktest
- URL: https://github.com/nkaz001/hftbacktest (ноутбук `examples/High-Frequency Grid Trading - Simplified from GLFT.ipynb`; live: `hftbacktest/examples/gridtrading_live_bybit.rs` + `algo.rs`)
- ★4.8k, MIT, последний коммит 2025-12-23; live только Rust (`connector/src/bybit`), есть Bybit-загрузчики данных (`hftbacktest.data.utils.bybit`).
- Логика (из `algo.rs`/ноутбука): каждые 100 мс: `mid`, `micro_price`; волатильность = std изменений mid за 10 мин; `half_spread = vol * vol_to_half_spread`; `bid_depth = half_spread*(1+skew*pos_norm)`, `ask_depth = half_spread*(1-skew*pos_norm)`; сетка из `grid_num` GTX (post-only) ордеров с шагом `grid_interval`, цены ограничены `min(bid, best_bid)`/`max(ask, best_ask)`; ордера вне новой сетки отменяются, недостающие ставятся; `max_position`.
- Тип: HFT-грид / MM без альфы; тик-уровень; perp, обе стороны; Freq: сотни–тысячи.
- Фреймворк: hftbacktest (Rust live); Порт: med (сборка Rust, iceoryx IPC, ключи; paper-режима нет → testnet или shadow-mode).
- Оценка: авторы прямо пишут: «без альфы стратегия сильно зависит от ребейтов + краткосрочной реверсии, особенно в альтах». Бэктесты в репо считаны при ребейте −0.0025% (Bybit MM-программа); при +0.02% VIP0 большая часть прибыли исчезает. Лучший в мире открытый инструмент, чтобы **проверить это до paper-трейдинга** (очередь, латентность, Bybit L2-данные).

#### B9. hftbacktest — Queue-Based Market Making в активах с большим тиком
- URL: https://github.com/nkaz001/hftbacktest/blob/master/examples/Queue-Based%20Market%20Making%20in%20Large%20Tick%20Size%20Assets.ipynb
- Логика (из кода): `half_spread = 0.49*tick`, `grid_interval = tick`, `book_pressure = (bid*ask_qty + ask*bid_qty)/(bid_qty+ask_qty)` (микроцена), опционально `trade_impulse` по последним сделкам; `reservation = book_pressure - skew*pos_norm`; котировки на BBO, вариант «pure queue model» — держать ордер в очереди на лучшей цене и снимать при падении объёма за спиной ниже `qty_threshold`. Пример на CRVUSDT (тик = 38 б.п.).
- Тип: очередной MM (queue-position) в large-tick альтах; тик-уровень; perp; Freq: очень высокая.
- hftbacktest (Rust live); Порт: med.
- Оценка: **единственная HFT-идея, у которой есть структурный запас при VIP0**: заработок 1 тик (десятки б.п.) против 4 б.п. комиссий за цикл; всё решает приоритет в очереди и adverse selection. На Bybit есть подходящие символы (tick/price ≥ 10 б.п.). Требует оценки позиции в очереди (hftbacktest это моделирует).

#### B10. BitMEX `sample-market-maker` — BitMEX/sample-market-maker
- URL: https://github.com/BitMEX/sample-market-maker
- ★1.7k, Apache-2.0, последний коммит 2023-05-24 (архивный статус фактически)
- Логика (`market_maker.py`): `start_position_buy = ticker.buy + tick`, `start_position_sell = ticker.sell - tick`; при `MAINTAIN_SPREADS` — `ORDER_PAIRS` уровней с шагом `INTERVAL` от стартовых цен; принудительный `MIN_SPREAD`; `converge_orders()` амендит существующие ордера вместо отмены; лимиты `MIN_POSITION/MAX_POSITION`; цикл `LOOP_INTERVAL=5s`.
- Тип: наивный симметричный лэддер; perp (inverse BitMEX); Freq: десятки–сотни.
- Фреймворк: custom; Порт: high (весь BitMEX REST/WS-обвес заменить на pybit v5; логика ~400 строк).
- Оценка: исторический шаблон, без альфы, без контроля инвентаря кроме лимитов. Смысла портировать нет — B1 даёт то же на Bybit из коробки. В каталоге как reference.

#### B11. beatzxbt/smm (ранее bybit-smm) — beatzxbt/smm
- URL: https://github.com/beatzxbt/smm
- ★610, MIT, последний коммит 2024-04-20 (v2 «exchange-agnostic» — WIP); клон Runner000/Simple-Bybit-Automated-Trader (0★) — тот же код.
- Логика (из `src/strategy/marketmaker.py`, `features/generate.py`): фичи — `bybit_mark_wmid_spread`, `binance_bybit_wmid_spread` (лид-лаг Binance→Bybit), `bba_imbalance`, `orderbook_imbalance`, `trades_imbalance`, `wmid_vamp_spread` → взвешенный `skew ∈ [-1,1]`; `spread = base_spread * clip(vol_multiplier, 1, 10)` (волатильность = ширина BB(20, 2.5)); котировки обеих сторон смещаются по skew (`best_bid = best_ask - 0.33*spread` при bid_skew ≥ ask_skew), при `|inventory_delta| ≥ inventory_extreme=0.5` — reduce-only одна сторона; размеры также скошены; asyncio WS Bybit (UTA) + Binance.
- Тип: MM с микроструктурными фичами и кросс-биржевым сигналом; тик-уровень; perp; Freq: очень высокая (непрерывный requote).
- Фреймворк: custom asyncio (Bybit-native); Порт: low–med (проверить совместимость с API v5/UTA 2026, обновить WS-эндпоинты).
- Оценка: лучший «маленький» открытый MM именно для Bybit; лид-лаг Binance→Bybit — реальная микроструктурная альфа. Нет учёта комиссий, автор честно пишет «вряд ли прибыльно». Для стенда с публичным WS Bybit — естественный кандидат на кастомный движок.

#### B12. verata-veritatis/bybit-market-maker
- URL: https://github.com/verata-veritatis/bybit-market-maker
- ★52, MIT, последний коммит 2021-03-24 (pybit v1, inverse BTCUSD)
- Логика (`config.py`/`run.py`): `NUM_ORDERS=20` лонгов и шортов равномерно в `RANGE=4%` от last; при заполнении одной стороны отменяет другую и ставит TP на `TP_DIST=0.3%`, SL `STOP_DIST=2.5%`; после закрытия цикл повторяется.
- Тип: по сути DCA-лэддер с TP, не MM; perp; Freq: 5–30.
- Custom; Порт: med (API v1→v5, inverse→linear).
- Оценка: без edge, SL 2.5% против TP 0.3% — плохая асимметрия. Не тестировать.

#### B13. Простые грид-боты для Bybit: r0mik/gridbot, Seynro/ByBit-Grid-Trading-Bot
- URL: https://github.com/r0mik/gridbot (★1, MIT, 2025-11-30; pybit v5, spot+futures, веб-дашборд, арифметический грид, refill противоположного уровня); https://github.com/Seynro/ByBit-Grid-Trading-Bot (★4, MIT, 2024-11-17; ccxt spot, геометрический грид, ATR/RSI-подстройка, 160 строк).
- Тип: грид; Freq: зависит от шага.
- Custom; Порт: low (уже Bybit).
- Оценка: без бэктеста, без риск-менеджмента; та же логика есть в B5/B7 с несравнимо лучшей инженерией. Не тестировать.

#### B14. Реализации Avellaneda–Stoikov «для чтения»: fedecaccia/avellaneda-stoikov, Jungle-Sven/avellaneda_stoikov_mm
- URL: https://github.com/fedecaccia/avellaneda-stoikov (★729, лицензии нет, 2020-05-02) — чистая симуляция на броуновском движении (γ=0.1, σ=2, k=1.5), без биржи; https://github.com/Jungle-Sven/avellaneda_stoikov_mm (★13, 2022-08-21) — класс с формулами `r = mid - q*γ*σ²*(T-t)`, `spread = (2/γ)*ln(1+γ/k)`, абстрактные `read_sigma_value()`/`gamma_calculation()`; комментарии на русском.
- Оценка: не торговые боты; полезны как компактные формулы для кастомного движка. Рабочая AS — это B2.

#### B15. pranay123-stack/crypto-hft-market-making-system
- URL: https://github.com/pranay123-stack/crypto-hft-market-making-system
- ★2, MIT, 2026-03-05, 3 коммита; шаблоны C++/Rust/Python; `python-single-exchange/src/strategy/market_maker.py`: `BasicMarketMaker` (target 10 б.п., min 5/max 50, масштаб по волатильности, `inventory_skew=0.5`) и `AvellanedaStoikovMM(γ=0.1, σ=0.01)`; Bybit-адаптер есть только в multi-exchange варианте.
- Оценка: выглядит как сгенерированный boilerplate без истории и тестов. Не тестировать.

#### B16. NautilusTrader примеры: `grid_mm`, `composite_market_maker` (Rust) + Python `GridMarketMakerConfig`
- URL: https://github.com/nautechsystems/nautilus_trader/tree/develop/crates/trading/src/examples/strategies ; Python-пример `examples/backtest/fx_market_maker_gbpusd_bars.py` (`from nautilus_trader.trading import GridMarketMakerConfig`)
- ★29.4k, LGPL-3.0, последний коммит 2026-09-26; Bybit-адаптер «stable» (LINEAR/SPOT/INVERSE/OPTION), есть `SandboxExecutionClient` для paper-исполнения на живых данных.
- Логика `grid_mm` (README): геометрический грид `buy_n = mid*(1-bps/1e4)^n - skew`, `sell_n = mid*(1+bps/1e4)^n - skew`, `skew = skew_factor*net_position`, `num_levels=3`, `grid_step_bps=10`, requote только при сдвиге mid ≥ `requote_threshold_bps=5`, `max_position` с учётом worst-case открытых ордеров, опц. GTD. `composite_market_maker`: один bid/ask вокруг mid с инвентарным скосом + скос от внешнего сигнала (SyntheticInstrument), post_only.
- Тип: грид-MM с инвентарным скосом; тик-уровень; perp/spot; Freq: десятки–сотни при шаге 10 б.п.
- Фреймворк: Nautilus; Порт: low (сменить instrument/venue на BYBIT).
- Оценка: стратегии сами по себе «учебные», но это самый инженерно надёжный путь получить paper-MM на Bybit с честной симуляцией исполнения. `composite_market_maker` с сигналом «Binance-perp mid» = аналог идеи B11 в production-движке.

### (c) Тик/стакан микроструктура

#### C1. hftbacktest — Market Making with Alpha: Order Book Imbalance
- URL: https://github.com/nkaz001/hftbacktest/blob/master/examples/Market%20Making%20with%20Alpha%20-%20Order%20Book%20Imbalance.ipynb
- Логика (из кода `obi_mm`): каждый `interval` суммирует объём bid/ask в пределах `looking_depth` от mid; `imbalance = Σbid − Σask`; `alpha = z-score(imbalance, window)`; `fair = mid + c1*alpha`; `reservation = fair − skew*pos_norm`; `bid = min(round(reservation − half_spread), best_bid)`, `ask = max(…, best_ask)`; сетка из `grid_num` GTX-ордеров; лимит по `max_position_dollar`.
- Тип: MM с OBI-альфой; тик-уровень; perp; Freq: тысячи.
- hftbacktest (Rust live); Порт: med.
- Оценка: авторский ретест 2025: «OBI продолжает работать стабильно, но доход на сделку упал с 0.0139% до 0.0086% **включая ребейт 0.005%**». На Bybit VIP0 (+0.02% maker) чистый результат отрицательный без усиления альфы/расширения спреда. Обязательно прогнать в бэктестере с Bybit-комиссиями (данные Bybit L2 качаются их же утилитами) — дёшево и показательно.

#### C2. NautilusTrader `OrderBookImbalance` (tutorial + Bybit ob500 backtest)
- URL: https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/tutorials/orderbook_imbalance.py ; https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/tutorials/backtest_orderbook_bybit.py
- Логика (из кода): на каждом `on_book_deltas`: `bid_size/ask_size` на лучших уровнях; если `larger > trigger_min_size` и `smaller/larger < trigger_imbalance_ratio` (0.20 → перевес 5:1) и прошло ≥ `min_seconds_between_triggers` (1с) — лимитный ордер по цене противоположной стороны (`best_ask` для покупки, т.е. кросс = taker) размером `min(level_size, max_trade_size)`.
- Тип: taker-momentum по дисбалансу BBO; event-driven; perp; Freq: десятки–сотни (троттлинг 1с).
- Nautilus; Порт: low (уже есть Bybit ob500-загрузчик и live-адаптер).
- Оценка: дисбаланс на BBO предсказывает ~полтика, а taker-цикл стоит 0.11% — на BTC заведомо в минус. Ценность: готовый harness Bybit L2 → стратегия → sandbox; можно переделать во **maker-вариант** (вставать в очередь на «тяжёлой» стороне) — тогда это C-версия B9.

#### C3. dineshpinto/orderbook-delta-bot
- URL: https://github.com/dineshpinto/orderbook-delta-bot
- ★80, Apache-2.0, последний коммит 2022-11-08 (FTX, архив)
- Логика (`main.rs`): каждые `time_delta` сек `bid_ask_delta = Σbid_vol − Σask_vol` до заданной глубины; BB(20, 2σ) по ряду дельты; `delta > bb.upper → SHORT`, `delta < bb.lower → LONG` (контр-трейд перекоса), TP/SL триггер-ордерами.
- Тип: контр-трендовый по перекосу стакана; ~секундные семплы; perp; Freq: 10–50.
- Фреймворк: custom; Порт: high (FTX мёртв; переписать ~200 строк на Python/pybit).
- Оценка: знак сигнала противоположен C1/C2 (те торгуют по дисбалансу, этот — против). Автор в отдельном репо анализа показал маргинальный результат; taker-исполнение съедает всё. Полезно как дешёвый «вариант сигнала» в кастомном движке рядом с OBI-maker-версией, не как самостоятельный кандидат.

#### C4. NautilusTrader `HurstVpinDirectional` (Rust; tutorial Kraken Futures)
- URL: https://github.com/nautechsystems/nautilus_trader/tree/develop/crates/trading/src/examples/strategies/hurst_vpin_directional ; docs/tutorials/hurst_vpin_kraken.md
- Логика: долларовые бары; Hurst на 128 барах как режимный фильтр (`≥0.55` тренд), VPIN по агрессору сделок (`≥0.30`), знак — по подписанному дисбалансу; вход market IOC; выход при `Hurst < 0.50` или таймаут.
- Тип: directional по токсичности потока; event-driven; perp; Freq: единицы/день.
- Nautilus; Порт: low (сменить venue на Bybit). Оценка: taker и низкая частота — не под задачу «много сделок», но идея режимного фильтра переносима на другие стратегии.

(Не вошли: hcobimtz-cloud/crypto-microstructure-trader и python-telegramBot/crypto-liquidity-ai-trading-bot — на дату исследования 404.)

### (d) Статарб / пары / funding / basis

#### D1. Hummingbot `spot_perpetual_arbitrage` (v1)
- URL: https://github.com/hummingbot/hummingbot/blob/master/hummingbot/strategy/spot_perpetual_arbitrage/spot_perpetual_arbitrage.py
- Логика: два `ArbProposal` (buy spot + sell perp / sell spot + buy perp); открытие при `profit_pct() ≥ min_opening_arbitrage_pct`, закрытие при обратном спреде ≥ `min_closing_arbitrage_pct`; между ними позиция собирает funding; slippage-буферы, market-ордера, проверка бюджета обеих ног.
- Тип: basis/funding-арбитраж spot↔perp на одной бирже (Bybit spot + bybit_perpetual); event-driven; Freq: 0–3 (позиции живут часы–дни).
- Hummingbot; Порт: none.
- Оценка: структурная альфа (funding) реальна, но: вход+выход = spot 0.1%×2 + perp 0.055%×2 ≈ 0.31%; при BTC-funding ~0.01%/8ч окупается ~неделю; смысл только на альтах с funding ≥0.05%/8ч. Не «много сделок», но единственный класс с положительным ожиданием «по построению».

#### D2. Hummingbot `scripts/v2_funding_rate_arb.py`
- URL: https://github.com/hummingbot/hummingbot/blob/master/scripts/v2_funding_rate_arb.py
- Логика: для набора токенов выбирает пару коннекторов с максимальной разницей нормализованного funding (`get_most_profitable_combination`), вход при `funding_rate_diff ≥ min_funding_rate_profitability=0.1%/день` через два PositionExecutor (лонг там, где funding ниже, шорт — где выше), TP при `pnl + funding ≥ profitability_to_take_profit=1%`, SL при `funding_rate_diff < −0.1%`; опц. проверка `get_current_profitability_after_fees`.
- Тип: кросс-биржевой funding-арб (Hyperliquid vs Binance по умолчанию); Freq: <1.
- Hummingbot; Порт: med (заменить одну ногу на Bybit spot — по сути превращается в D1 на V2-архитектуре).
- Оценка: кросс-биржевой вариант нам не нужен; ценен как современный V2-шаблон для D1.

#### D3. Hummingbot V2 `controllers/generic/stat_arb.py`
- URL: https://github.com/hummingbot/hummingbot/blob/master/controllers/generic/stat_arb.py
- Логика (из кода): два инструмента на одном коннекторе; `lookback_period=300` свечей; регрессия кумулятивных доходностей hedge-ноги на dominant-ногу; `spread_pct = (hedge_cum − y_pred)/y_pred*100`, `z = (spread − mean)/std`; `z > entry_threshold=2.0` → шорт спреда, `z < −2` → лонг спреда; входы лимитными «quoter»-ордерами по `min/max price*(1±quoter_spread=0.01%)`; `pos_hedge_ratio`, тройной барьер, сокращение при противоположном сигнале.
- Тип: pairs trading (z-score), maker-входы; свечной (интервал конфигурируется); perp; Freq: 1–5 циклов/день на пару.
- Hummingbot; Порт: none (`bybit_perpetual`).
- Оценка: приятный сюрприз — готовый парный контроллер в основном репо. Maker-входы решают проблему комиссий; риск — разрыв коинтеграции и то, что z считается на 300 наблюдений без теста стационарности. Подбор пар — через D6.

#### D4. kiprella/Funding-rate-arbitrage-bot
- URL: https://github.com/kiprella/Funding-rate-arbitrage-bot
- ★0, MIT, последний коммит 2025-05-05; Bybit-only (spot+perp) delta-neutral; фактически монитор funding через REST с логированием «возможностей», исполнения нет.
- Оценка: скелет; пригоден только как справочник по Bybit-эндпоинтам funding.

#### D5. aoki-h-jp/funding-rate-arbitrage
- URL: https://github.com/aoki-h-jp/funding-rate-arbitrage
- ★309, MIT, последний коммит 2023-11-05; ccxt-скринер (binance/bybit/okx/gate/coinex/bitget): `display_large_divergence_single_exchange('bybit')` считает `Revenue = FR − Commission(0.32%)` — в README все примеры по Bybit **отрицательные**; исполнения нет.
- Оценка: не бот, но наглядно демонстрирует фи-проблему D1. Использовать как скринер для выбора монет под D1.

#### D6. olamide05/statarb-bot
- URL: https://github.com/olamide05/statarb-bot
- ★0, лицензия не указана, последний коммит 2026-09-26; Engle–Granger скан пар через ccxt (публичные данные Binance), OLS hedge ratio, rolling z-score с entry/exit/stop, walk-forward бэктест с комиссиями и slippage, paper-режим по опросу цен; исполнения нет.
- Оценка: чистый research-скаффолд; подходит как «слой отбора пар» для D3 (сменить exchange на bybit в ccxt). Отсутствие лицензии — юридический вопрос.

#### D7. hftbacktest — Market Making with Alpha: Basis
- URL: https://github.com/nkaz001/hftbacktest/blob/master/examples/Market%20Making%20with%20Alpha%20-%20Basis.ipynb
- Логика: `basis = perp_mid − spot_mid`, альфа = отклонение basis от скользящего среднего (предполагается стационарность basis), котировки MM скошены по альфе; данные L1 spot (BTCFDUSD) + perp.
- Тип: MM с basis-альфой; тик-уровень; perp (+spot-фид); Freq: тысячи.
- hftbacktest (Rust live); Порт: med (на Bybit spot и linear идут по одному WS).
- Оценка: та же оговорка про ребейт, что C1; интересна как второй источник альфы поверх B8/C1.

(Исключено: 50shadesofgwei/funding-rate-arbitrage — ★183, MIT, но GMX/Synthetix/HMX/Binance — DEX-ориентировано, к Bybit не относится.)

### (e) Самостоятельные боты (ccxt/pybit) и примеры фреймворков

#### E1. ryu878/bybit_scalp_bot (+ форк mzaeemnasir/ByBit-Scalping-Bot)
- URL: https://github.com/ryu878/bybit_scalp_bot
- ★20 (форк 0★), MIT, последний коммит 2026-06-25 (правки README; код 2022, `pybit==2.4.1` — устаревший API v2)
- Логика (из `xrp.py`): 1m; только шорт: `if ask > EMA6(high) and Stoch(15) > 80 (main и signal выше порогов)` → лимитный sell на `ema6hgh + (ema6hgh − ema6low)`; довход x2, если `ema6low > entry` (усреднение против позиции); TP лимиткой на `ema6hgh − (ema6hgh − ema6low)`; SL нет.
- Тип: фейд импульса лимитками + мартингейл; 1m; perp short only; Freq: 10–50.
- Custom; Порт: med (переписать на pybit v5).
- Оценка: типичный GitHub-«скальпер»: maker-входы (плюс), но без стопа — гарантированный blow-up на памп-свече. Не тестировать в исходном виде; идея «лимитный фейд + TP» покрывается B3.

#### E2. CryptoGnome/Bybit-Futures-Bot (Liquidation Hunting + DCA)
- URL: https://github.com/CryptoGnome/Bybit-Futures-Bot
- ★121, MIT, последний коммит 2022-02-17 (bybit-api v1; форк SingFan/liquidation_hunting)
- Логика (`BybitUSDT/websocket.py`, `profit.py`): подписка на поток ликвидаций Bybit (`check_liquidations`), контр-трейд ликвидации **market**-ордером (`order_type="Market"`), 4 уровня DCA по `dca_drawdown_percent_N` с множителями `dca_size_multiplier_N`, TP лимиткой на `take_profit_percent`; SL нет.
- Тип: фейд каскадов ликвидаций; event-driven; perp long/short; Freq: 10–50 на портфель в волатильные дни.
- Custom; Порт: med–high (API v5: публичный стрим `allLiquidation.SYMBOL` доступен без ключей, логика 300 строк).
- Оценка: у идеи есть микроструктурное основание — ликвидации это принудительный неинформированный поток, после каскадов есть реверсия. Реализация плохая (taker + мартингейл без стопа). Стоит **переписать** в кастомном движке: лимитные ордера под кластер ликвидаций, жёсткий SL, без DCA.

#### E3. shalom-ars/crypto-futures-trading-bot
- URL: https://github.com/shalom-ars/crypto-futures-trading-bot
- ★0, лицензии нет, последний коммит 2026-09-24; asyncio + ccxt.pro, Binance/Bybit USDT-M, есть dry-run.
- Логика (README + `scoring.py`): 15m; счёт −100…+100: EMA20/50/200 (±30), RSI14+MACD-гистограмма (±25), объём vs SMA20 ×1.8 (±25), funding + OI-моментум (±20); long при ≥+70, short при ≤−70; SL 5% market, trailing-TP активируется на +0.5% и выходит при откате 0.8%; 30 слотов, 1% эквити, 5x.
- Тип: мультифакторный momentum; 15m; perp long/short; Freq: 1–3 на символ (30–90 на портфель).
- Custom asyncio; Порт: low (уже Bybit).
- Оценка: SL 5% против типичного выигрыша ~0.5–1% при таker-цикле 0.11% — отрицательная асимметрия; свежий код без лицензии и без бэктеста. Ценен как готовый asyncio-скелет Bybit (WS-тикер, trailing-воркеры), не как стратегия.

#### E4. Haehnchen/crypto-trading-bot (TypeScript/Node)
- URL: https://github.com/Haehnchen/crypto-trading-bot
- ★3.5k, MIT, последний коммит 2026-08-02; биржи вкл. Bybit (linear), бэктест, веб-UI, paper через свой движок.
- Стратегии (`src/strategy/strategies`): `cci` (CCI14 возврат из зоны ±100 с фильтром SMA200/EMA200), `cci_macd`, `macd`, `awesome_oscillator_cross_zero`, `obv_pump_dump`, `parabolicsar`, `pivot_reversal_strategy`, `dip_catcher` (канал HMA high/low + HMA), `dca_dipper`; watchdogs: stoploss, trailing_stop, risk_reward_ratio.
- Тип: индикаторные кроссы; обычно 15m–1h; perp long/short; Freq: 0.5–3.
- Фреймворк: собственный (JS); Порт: none в его движке / low при переносе правил во Freqtrade.
- Оценка: живой проект с Bybit, но стратегии — учебные, edge не заявляется. Для Python-лаборатории смысл — только перенести 1–2 правила (`dip_catcher`, `cci`) во Freqtrade.

#### E5. jesse-ai/example-strategies
- URL: https://github.com/jesse-ai/example-strategies
- ★178, MIT, последний коммит 2024-03-21; Jesse поддерживает Bybit USDT-perp live/paper (live-плагин платный).
- Логика `RSI2` (из кода): `should_long: price > SMA200 & RSI(2) ≤ 10`; `should_short: price < SMA200 & RSI(2) ≥ 90`; выход лонга при `price > SMA5` (и зеркально). Также `SimpleBollinger`, `MACD_EMA`, `DUAL_THRUST`, `Donchian`, `TurtleRules`, `KDJstrategy`, `IFR2`, `MAGen`, `SMACrossover`, `TradingView_RSI`.
- Тип: учебные; таймфрейм задаётся в routes (RSI2 применим на 5m–1h); perp long/short; Freq (RSI2, 5m): 1–4.
- Фреймворк: Jesse; Порт: low при переносе во Freqtrade (10 строк).
- Оценка: RSI2 — одна из немногих «классических» реверсий с некоторой устойчивостью в трендовых режимах; на 5m тонко по комиссиям. Годится как второй Freqtrade-кандидат после E0V1EN.

#### E6. NautilusTrader `ema_cross` семейство (Python tutorial + Rust)
- URL: https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/tutorials/ema_cross.py ; `examples/backtest/crypto_ema_cross_ethusdt_trade_ticks.py`
- Логика: кросс быстрой/медленной EMA, market-ордера; bracket-вариант с ATR SL/TP; TWAP-вариант.
- Тип: тренд-кросс; любые бары/тики; perp; Freq: на 1m 10–30.
- Nautilus; Порт: low. Оценка: шум; использовать как smoke-test Bybit live/sandbox перед B16/C2.

---

## 2. Top-15 для первоочередного тестирования

Критерии: частота сделок → правдоподобие edge (maker, реверсия в ликвидных рынках, структурные эффекты) → лёгкость запуска на Bybit → качество кода.

| # | Кандидат | Обоснование (одна строка) |
|---|---|---|
| 1 | **B9** hftbacktest Queue-Based MM (large-tick альты) | Единственная HFT-идея с запасом при VIP0: 1 тик (десятки б.п.) против 4 б.п. комиссий, очередь моделируется. |
| 2 | **B1** Hummingbot `pure_market_making` на `bybit_perpetual` | Обязательный maker-baseline, тысячи пользователей, ничего портировать. |
| 3 | **B7** passivbot `trailing_martingale` (long+short, Forager) | Bybit-first, pure maker, бэктест+оптимизатор+`fake_live`; эталон грид/DCA. |
| 4 | **D3** Hummingbot V2 `stat_arb` | Готовый парный контроллер с maker-входами на Bybit perps; статистическая, а не индикаторная альфа. |
| 5 | **B11** beatzxbt/smm | Bybit-native MM с лид-лагом Binance→Bybit и OB/трейд-дисбалансами — реальная микроструктурная фича. |
| 6 | **C1** hftbacktest OBI-MM | Лучший открытый пример альфа-MM; сначала бэктест с Bybit-комиссиями (авторы показали, что без ребейта тонко). |
| 7 | **B8** hftbacktest GLFT-грид (`gridtrading_live_bybit.rs`) | Готовый live-код для Bybit; проверка гипотезы «реверсия в альтах покрывает 4 б.п.». |
| 8 | **B5** Hummingbot `grid_strike`/`bollingrid` | Maker/maker грид 0.1% с TP LIMIT_MAKER; много сделок на боковых альтах. |
| 9 | **B3** Hummingbot `perpetual_market_making` | MM под перпы с TP/SL позиции; естественный второй baseline к B1. |
| 10 | **A11** Freqtrade E0V1EN | Самая популярная простая community-стратегия 2023–2026, 146 строк, 5m, запускается за час. |
| 11 | **A3** Freqtrade BinHV45 + зеркальный short | Событийная реверсия после резких проколов на 1m — единственный «содержательный» 1m-кандидат. |
| 12 | **E2'** Liquidation-fade (переписать идею CryptoGnome) | Структурный эффект (принудительный поток), публичный стрим ликвидаций доступен; нужны лимитки + SL. |
| 13 | **B2** Hummingbot `avellaneda_market_making` | Контроль инвентаря против B1 на тех же символах — дёшево и информативно. |
| 14 | **B16** Nautilus `grid_mm` + Bybit adapter + SandboxExecutionClient | Инженерно самый честный paper-режим; база для maker-версии C2 и `composite_market_maker`. |
| 15 | **D1** Hummingbot `spot_perpetual_arbitrage` | Мало сделок, но положительное ожидание по построению на альтах с высоким funding. |

Запасные: A4/ClucHAnix_5m, A8 NFI X8 (futures+short), B4 pmm_dynamic, C2 Nautilus OBI (как maker-вариант), E5 RSI2, A1 Scalp (негативный контроль).

---

## 3. Порядок тестирования по движкам

Идея: один движок — одна конфигурация подключения к Bybit, несколько стратегий по очереди/параллельно.

### Этап 1. Freqtrade (Bybit futures, `dry_run: true`, isolated, 1x–3x)
Одна установка, `config.json` с `exchange: bybit`, `trading_mode: futures`. Стратегии — отдельные инстансы на одном наборе пар (15–20 ликвидных USDT-перпов):
1. A11 `E0V1EN` (5m) — быстрый старт, проверка стенда.
2. A3 `BinHV45` (1m) + добавить `can_short` и зеркальные условия.
3. A4 `ClucHAnix_5m` / `CombinedBinHAndCluc` (5m).
4. A6 `FReinforcedStrategy` (5m, long/short) — проверка исполнения шортов.
5. A8 NFI X8 (`is_futures_mode=True`, конфиг `pairlist-static-bybit-futures-usdt.json`) — тяжёлая, запускать последней и отдельно.
6. A1 `Scalp` — как негативный контроль (ожидаемо в минус; калибрует оценку комиссий стенда).
Позже: E5 RSI2 и E4 `dip_catcher`, перенесённые в 10-строчные Freqtrade-классы.

### Этап 2. Hummingbot (коннекторы `bybit_perpetual` / `bybit`; paper через `paper_trade` для спота и testnet-домен для перпов)
1. B1 `pure_market_making` (спред 0.05–0.1%, refresh 15с, 2–3 символа разной ликвидности: BTC, средний альт, large-tick альт).
2. B2 `avellaneda_market_making` — те же символы, сравнение с B1.
3. B3 `perpetual_market_making`.
4. B4 V2 `pmm_dynamic` со сжатыми спредами (0.1–0.3%, TP 0.1–0.2%).
5. B5 `grid_strike`/`bollingrid` на 2–3 боковых альтах.
6. D3 `stat_arb` на 2–3 парах, отобранных D6/D5-скринерами.
7. D1 `spot_perpetual_arbitrage` на 1–2 альтах с высоким funding (фоновый, низкочастотный).

### Этап 3. hftbacktest (сначала бэктест на Bybit L2-данных с `trading_value_fee_model(0.0002, 0.00055)`, затем Rust-live на testnet/shadow)
1. B9 Queue-based MM — выбрать 2–3 символа Bybit с tick/price ≥ 10 б.п.
2. B8 GLFT-грид (`gridtrading_live_bybit.rs`).
3. C1 OBI-MM (те же символы, сравнить с B8: даёт ли альфа прибавку, покрывающую отсутствие ребейта).
4. D7 Basis-MM (spot+perp Bybit) — только если C1 показал положительный результат.

### Этап 4. NautilusTrader (Bybit LINEAR adapter + `SandboxExecutionClient`)
1. E6 `ema_cross` — smoke-test подключения.
2. B16 `grid_mm` (Python `GridMarketMakerConfig`).
3. C2 `OrderBookImbalance` в исходном (taker) виде как контроль, затем maker-вариант (постановка в очередь на тяжёлой стороне).
4. B16 `composite_market_maker` с сигналом «Binance-perp mid» (аналог B11 в production-движке).
5. C4 `HurstVpinDirectional` — низкоприоритетно.

### Этап 5. Кастомный asyncio-движок (pybit v5 + публичный WS `wss://stream.bybit.com/v5/public/linear`, симулятор исполнения)
1. B11 beatzxbt/smm — обновить эндпоинты, подключить симулятор филлов.
2. E2' Liquidation-fade: стрим `allLiquidation.SYMBOL` → лимитные ордера против каскада, жёсткий SL, без DCA.
3. C3-вариант: BB на дельте стакана как альтернативный знак сигнала в том же движке.
4. D4/D5 как скринеры funding для этапа 2.7.

### Этап 6. passivbot (собственный движок, режим `fake_live`/testnet)
1. B7 `trailing_martingale` long (конфиг `configs/examples/default_trailing_martingale_long.json`), затем short, затем оба с Forager на 10–15 монетах.

---

## 4. Замечания и неожиданности

1. **Комиссии убивают почти всё «индикаторное».** Из 11 Freqtrade-кандидатов только те, у кого цели ≥1% и maker-входы, переживают 0.11% taker-цикл; 1m-скальперы (A1, A2) — негативные контроли.
2. **Hummingbot тихо вырос**: в основном репо появились `controllers/generic/stat_arb.py` (парный z-score на одной бирже), `pmm_mister`, `bollingrid`, `multi_grid_strike`, `quantum_grid_allocator`, `hedge_asset` — про них почти нет упоминаний в блогах.
3. **hftbacktest честно предупреждает**: ретест OBI-MM за 2025 г. дал 0.0086% на сделку *с учётом ребейта 0.005%*; для VIP0 (+0.02% maker) это минус. Ставка на B9 (large-tick, доход = 1 тик) — единственный HFT-путь без ребейта.
4. **NFI X8 полноценно поддерживает futures и шорты** (32 short-условия, `can_short=True` в futures-режиме, готовые Bybit-futures пейрлисты) — README об этом не говорит.
5. **Nautilus перенёс примеры стратегий**: Python-примеры теперь в `docs/tutorials/*.py` и `examples/backtest`, а полноценные — в Rust (`crates/trading/src/examples/strategies`: `grid_mm`, `composite_market_maker`, `hurst_vpin_directional`, `delta_neutral_vol`); из Python доступен `GridMarketMakerConfig`.
6. **«Bybit scalper»-репозитории практически все мертвы** (pybit v2 2022 г., 0–50★, без SL); два результата поиска (crypto-microstructure-trader, crypto-liquidity-ai-trading-bot) уже удалены. Живые Bybit-native проекты — passivbot, beatzxbt/smm, Haehnchen (JS).
7. **Funding-арбитраж на одной бирже** по скринеру aoki-h-jp для Bybit почти всегда фи-отрицателен; смысл есть только на альтах с экстремальным funding и при хранении позиции ≥ недели.
8. **ssssi/freqtrade_strs (651★)** — самый популярный community-репо Freqtrade на 2026 г.; стратегия E0V1EN — 146 строк и market-ордера; есть `BinHV27_short.py` с `can_short`.
9. **passivbot v8** перешёл на Rust-оркестратор и стратегию `trailing_martingale`; v7-конфиги не совместимы (есть мигратор).
10. Лицензии: большинство Freqtrade-стратегий — GPL-3.0 (сам Freqtrade GPL), Hummingbot Apache-2.0, hftbacktest MIT, Nautilus LGPL-3.0, passivbot Unlicense; у statarb-bot, ssssi/freqtrade_strs, fedecaccia/avellaneda-stoikov лицензии нет.
