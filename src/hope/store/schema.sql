-- Общая схема результатов для всех движков/фреймворков лаборатории.
-- Движок пишет через Store (отдельный поток, батчи); монитор читает параллельно (WAL).

CREATE TABLE IF NOT EXISTS runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    mode          TEXT NOT NULL,            -- live | backtest
    engine        TEXT NOT NULL DEFAULT 'hope',  -- hope | freqtrade | hummingbot | nautilus ...
    branch        TEXT DEFAULT '',
    started_ts    INTEGER NOT NULL,
    finished_ts   INTEGER,
    status        TEXT NOT NULL DEFAULT 'running',  -- running | finished | crashed | stopped
    initial_equity REAL NOT NULL,
    symbols       TEXT NOT NULL,            -- JSON-список
    config_json   TEXT NOT NULL,
    params_json   TEXT NOT NULL DEFAULT '{}',
    summary_json  TEXT DEFAULT NULL         -- итоговые метрики (заполняется при завершении)
);

CREATE TABLE IF NOT EXISTS orders (
    run_id        INTEGER NOT NULL,
    order_id      INTEGER NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    price         REAL NOT NULL,
    qty           REAL NOT NULL,
    filled        REAL NOT NULL,
    taker         INTEGER NOT NULL,
    purpose       TEXT NOT NULL,
    tag           TEXT DEFAULT '',
    ts_created    INTEGER NOT NULL,
    ts_done       INTEGER NOT NULL,
    status        TEXT NOT NULL,
    queue_ahead_initial REAL DEFAULT 0,
    spread_bps_at_place REAL DEFAULT 0,
    PRIMARY KEY (run_id, order_id)
);

CREATE TABLE IF NOT EXISTS fills (
    run_id        INTEGER NOT NULL,
    fill_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    seq           INTEGER NOT NULL DEFAULT 0,   -- порядковый номер исполнения в запуске
    order_id      INTEGER NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    price         REAL NOT NULL,
    qty           REAL NOT NULL,
    fee           REAL NOT NULL,
    ts            INTEGER NOT NULL,
    is_maker      INTEGER NOT NULL,
    purpose       TEXT NOT NULL,
    tag           TEXT DEFAULT '',
    bid           REAL DEFAULT 0,
    ask           REAL DEFAULT 0,
    placed_ts     INTEGER DEFAULT 0,
    mid_at_place  REAL DEFAULT 0,
    spread_bps_at_place REAL DEFAULT 0,
    queue_ahead_initial REAL DEFAULT 0,
    inventory_before REAL DEFAULT 0,
    realized_pnl  REAL DEFAULT 0,
    position_after REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_fills_run_ts ON fills(run_id, ts);
CREATE INDEX IF NOT EXISTS ix_fills_run_sym ON fills(run_id, symbol);

-- Снимки портфеля (equity curve)
CREATE TABLE IF NOT EXISTS equity (
    run_id        INTEGER NOT NULL,
    ts            INTEGER NOT NULL,
    equity        REAL NOT NULL,
    realized_pnl  REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    fees          REAL NOT NULL,
    funding       REAL NOT NULL DEFAULT 0,
    gross_notional REAL NOT NULL,
    n_positions   INTEGER NOT NULL,
    n_fills       INTEGER NOT NULL,
    n_open_orders INTEGER NOT NULL DEFAULT 0,
    max_drawdown  REAL NOT NULL,
    PRIMARY KEY (run_id, ts)
);

-- Текущее состояние позиций (перезаписывается)
CREATE TABLE IF NOT EXISTS positions (
    run_id        INTEGER NOT NULL,
    symbol        TEXT NOT NULL,
    qty           REAL NOT NULL,
    avg_price     REAL NOT NULL,
    mark          REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    realized_pnl  REAL NOT NULL,
    fees          REAL NOT NULL,
    funding       REAL NOT NULL DEFAULT 0,
    n_fills       INTEGER NOT NULL,
    opened_ts     INTEGER NOT NULL DEFAULT 0,
    updated_ts    INTEGER NOT NULL,
    PRIMARY KEY (run_id, symbol)
);

-- Статистика по символам (спред, волатильность, поток), периодические снимки
CREATE TABLE IF NOT EXISTS symbol_stats (
    run_id        INTEGER NOT NULL,
    ts            INTEGER NOT NULL,
    symbol        TEXT NOT NULL,
    mid           REAL,
    spread_bps    REAL,
    spread_med_bps REAL,
    vol_bps       REAL,
    trades_per_min REAL,
    turnover_per_min REAL,
    flow_imbalance REAL,
    PRIMARY KEY (run_id, ts, symbol)
);

-- Произвольные метрики стратегии (сигналы, индикаторы, состояния) — для монитора
CREATE TABLE IF NOT EXISTS metrics (
    run_id        INTEGER NOT NULL,
    ts            INTEGER NOT NULL,
    symbol        TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL,
    value         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_metrics_run_name ON metrics(run_id, name, ts);

CREATE TABLE IF NOT EXISTS events (
    run_id        INTEGER NOT NULL,
    ts            INTEGER NOT NULL,
    level         TEXT NOT NULL,
    symbol        TEXT DEFAULT '',
    msg           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_run_ts ON events(run_id, ts);

-- Рыночные данные (опционально, store.record_market = true)
CREATE TABLE IF NOT EXISTS md_bbo (
    run_id INTEGER NOT NULL, ts INTEGER NOT NULL, symbol TEXT NOT NULL,
    bid REAL, ask REAL, bid_qty REAL, ask_qty REAL
);
CREATE TABLE IF NOT EXISTS md_trades (
    run_id INTEGER NOT NULL, ts INTEGER NOT NULL, symbol TEXT NOT NULL,
    side TEXT, price REAL, qty REAL
);
-- Markout исполнений: mid через horizon_ms после исполнения; markout_bps = знак стороны * (mid - price)/price
CREATE TABLE IF NOT EXISTS markouts (
    run_id INTEGER NOT NULL, seq INTEGER NOT NULL, symbol TEXT NOT NULL,
    horizon_ms INTEGER NOT NULL, mid REAL NOT NULL, markout_bps REAL NOT NULL,
    PRIMARY KEY (run_id, seq, horizon_ms)
);
