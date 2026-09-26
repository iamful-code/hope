"""CLI: hope run | backtest | download | monitor | instruments."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import typer

app = typer.Typer(help="hope — лаборатория стратегий Bybit (paper, бэктест, монитор)", no_args_is_help=True, pretty_exceptions_enable=False)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def _load(config: list[Path] | None, symbols: str | None, set_: list[str] | None):
    from .config import load_config

    over: dict = {}
    for kv in set_ or []:
        k, _, v = kv.partition("=")
        cur = over
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        from .config import _coerce

        cur[parts[-1]] = _coerce(v)
    if symbols:
        over.setdefault("exchange", {})["symbols"] = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    return load_config(*(config or []), overrides=over)


@app.command()
def run(
    config: list[Path] = typer.Option(None, "--config", "-c", help="YAML стратегии (можно несколько, накладываются)"),
    symbols: str = typer.Option(None, "--symbols", "-s", help="Список символов через запятую"),
    duration: float = typer.Option(None, "--duration", help="Остановиться через N секунд"),
    name: str = typer.Option(None, "--name", help="Имя запуска"),
    warmup: int = typer.Option(None, "--warmup", help="Свечей прогрева (по умолчанию strategy.history_bars; 0 = без прогрева)"),
    set_: list[str] = typer.Option(None, "--set", help="Переопределить ключ: section.key=value"),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Запустить live paper-торговлю на публичном потоке Bybit."""
    _setup_logging(verbose)
    from .engine.live import run_live

    cfg = _load(config, symbols, set_)
    asyncio.run(run_live(cfg, duration, name, warmup))


@app.command()
def backtest(
    config: list[Path] = typer.Option(None, "--config", "-c"),
    symbols: str = typer.Option(None, "--symbols", "-s"),
    from_: str = typer.Option(None, "--from", help="YYYY-MM-DD"),
    to: str = typer.Option(None, "--to", help="YYYY-MM-DD"),
    name: str = typer.Option(None, "--name"),
    queue_usd: float = typer.Option(20_000.0, "--queue-usd", help="Оценка очереди впереди лимитного ордера, USDT"),
    funding: float = typer.Option(0.0001, "--funding", help="Ставка фандинга за 8ч в бэктесте"),
    db: str = typer.Option(None, "--db", help="Путь к БД результатов (по умолчанию store.db_path)"),
    mode: str = typer.Option("trades", "--mode", help="trades (каждая сделка, очередь) | candles (быстро, по свечам)"),
    set_: list[str] = typer.Option(None, "--set"),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Бэктест на архиве сделок public.bybit.com тем же движком и paper-брокером."""
    _setup_logging(verbose)
    from .engine.backtest import run_backtest

    cfg = _load(config, symbols, set_)
    summary = asyncio.run(run_backtest(cfg, from_, to, name, queue_usd, funding, db, mode=mode))
    summary.pop("instruments", None)
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


@app.command()
def replay(
    config: list[Path] = typer.Option(None, "--config", "-c"),
    source_db: str = typer.Option("data/hope.db", "--source-db", help="БД с записанными рыночными данными"),
    source_run: int = typer.Option(..., "--source-run", help="run_id запуска с store.record_market=true"),
    symbols: str = typer.Option(None, "--symbols", "-s"),
    name: str = typer.Option(None, "--name"),
    db: str = typer.Option(None, "--db", help="Куда писать результаты (по умолчанию store.db_path)"),
    set_: list[str] = typer.Option(None, "--set"),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Прогнать стратегию по записанным BBO и сделкам другого запуска (честная очередь для мейкерских стратегий)."""
    _setup_logging(verbose)
    from .engine.replay import run_replay

    cfg = _load(config, symbols, set_)
    syms = [x.strip().upper() for x in symbols.split(",")] if symbols else None
    summary = asyncio.run(run_replay(cfg, source_db, source_run, name, db, syms))
    typer.echo(json.dumps({k: v for k, v in summary.items() if k != "instruments"}, ensure_ascii=False, indent=2, default=str))


@app.command()
def download(
    symbols: str = typer.Option(..., "--symbols", "-s"),
    from_: str = typer.Option(..., "--from"),
    to: str = typer.Option(..., "--to"),
    category: str = typer.Option("linear", "--category"),
    data_dir: str = typer.Option("data", "--data-dir"),
) -> None:
    """Скачать архив сделок Bybit за период в кэш Parquet."""
    _setup_logging(False)
    from .bybit.history import TradeHistory

    h = TradeHistory(data_dir, category)
    missing = asyncio.run(h.ensure([s.strip().upper() for s in symbols.split(",")], from_, to))
    typer.echo(f"готово; отсутствуют: {missing or 'нет'}")


@app.command()
def monitor(
    db: str = typer.Option("data/hope.db", "--db", help="Путь к БД (или несколько через запятую)"),
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Веб-монитор результатов (FastAPI + Plotly), только чтение БД."""
    _setup_logging(False)
    import uvicorn

    from .monitor.app import create_app

    uvicorn.run(create_app(db), host=host, port=port, log_level="info")


@app.command()
def instruments(
    category: str = typer.Option("linear", "--category"),
    data_dir: str = typer.Option("data", "--data-dir"),
    rest_url: str = typer.Option("https://api.bybit.com", "--rest-url"),
) -> None:
    """Обновить кэш инструментов с REST (нужен доступ к api.bybit.com)."""
    _setup_logging(False)
    from .bybit.instruments import InstrumentCatalog
    from .bybit.rest import BybitRest

    async def go():
        rest = BybitRest(rest_url)
        cat = InstrumentCatalog(category, data_dir)
        try:
            src = await cat.load(rest)
        finally:
            await rest.close()
        typer.echo(f"источник={src}, инструментов={len(cat.metas)}, кэш={cat.cache_path}")

    asyncio.run(go())


@app.command()
def scan(
    duration: float = typer.Option(30.0, "--duration", help="Сколько секунд слушать тикеры"),
    min_turnover: float = typer.Option(5_000_000.0, "--min-turnover", help="Мин. оборот за 24ч, USDT"),
    min_tick_bps: float = typer.Option(0.0, "--min-tick-bps", help="Показать только символы с шагом цены >= N б.п."),
    category: str = typer.Option("linear", "--category"),
    ws_url: str = typer.Option("wss://stream.bybit.com/v5/public/linear", "--ws-url"),
    limit: int = typer.Option(60, "--limit"),
    out: str = typer.Option(None, "--out", help="Сохранить полную таблицу в CSV"),
) -> None:
    """Сканер символов по WebSocket (без REST): оборот, шаг цены в б.п., спред, фандинг."""
    _setup_logging(False)
    from .bybit.scan import archive_symbols, scan as _scan

    async def go():
        syms = await archive_symbols(category)
        typer.echo(f"кандидатов из архива: {len(syms)}; слушаю тикеры {duration:.0f} с ...")
        rows = await _scan(syms, ws_url, duration)
        rows = [r for r in rows if r.turnover_24h >= min_turnover and r.tick_bps >= min_tick_bps]
        typer.echo(f"{'symbol':14s} {'last':>12s} {'turnover24h,M':>14s} {'tick,bps':>9s} {'spread,bps':>11s} {'funding':>9s} {'OI,M':>8s}")
        for r in rows[:limit]:
            typer.echo(f"{r.symbol:14s} {r.last:12.6g} {r.turnover_24h/1e6:14.1f} {r.tick_bps:9.2f} {r.spread_bps:11.2f} {r.funding_rate:9.5f} {r.open_interest_value/1e6:8.1f}")
        if out:
            import csv

            with open(out, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["symbol", "last", "turnover_24h", "tick_bps", "spread_bps", "tick_est", "funding_rate", "open_interest_value", "n"])
                for r in rows:
                    w.writerow([r.symbol, r.last, r.turnover_24h, round(r.tick_bps, 3), round(r.spread_bps, 3), r.tick_est, r.funding_rate, r.open_interest_value, r.n])
            typer.echo(f"сохранено: {out}")

    asyncio.run(go())


@app.command("ft-export")
def ft_export(
    url: str = typer.Option("http://127.0.0.1:8080", "--url", help="Адрес REST API Freqtrade"),
    user: str = typer.Option(None, "--user", envvar="FT_API_USER", help="api_server.username (или env FT_API_USER)"),
    password: str = typer.Option(None, "--password", envvar="FT_API_PASSWORD", help="api_server.password (или env FT_API_PASSWORD)"),
    db: str = typer.Option("data/hope.db", "--db", help="БД результатов hope"),
    interval: float = typer.Option(10.0, "--interval", help="Период опроса API, с"),
    name: str = typer.Option(None, "--name", help="Имя запуска (по умолчанию ft:<стратегия>:<bot_name>)"),
    state: str = typer.Option(None, "--state", help="Файл состояния экспортёра (по умолчанию <db>.ftexport.json)"),
    once: bool = typer.Option(False, "--once", help="Один опрос и выход (запуск не завершается)"),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Экспорт сделок работающего Freqtrade (REST API) в БД hope как запуск engine=freqtrade; до Ctrl-C."""
    _setup_logging(verbose)
    import signal

    from .adapters.freqtrade.exporter import FreqtradeExporter

    exp = FreqtradeExporter(url, user, password, db, run_name=name, interval_secs=interval, state_path=state)

    async def go():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass  # Windows / не главный поток: остановка по KeyboardInterrupt ниже
        await exp.run(stop, once=once)

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        exp.finish("stopped")
    finally:
        exp.close()
    if once:
        typer.echo(json.dumps(exp.summary(), ensure_ascii=False))


if __name__ == "__main__":
    app()
