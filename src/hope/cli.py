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
    set_: list[str] = typer.Option(None, "--set", help="Переопределить ключ: section.key=value"),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Запустить live paper-торговлю на публичном потоке Bybit."""
    _setup_logging(verbose)
    from .engine.live import run_live

    cfg = _load(config, symbols, set_)
    asyncio.run(run_live(cfg, duration, name))


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
    set_: list[str] = typer.Option(None, "--set"),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Бэктест на архиве сделок public.bybit.com тем же движком и paper-брокером."""
    _setup_logging(verbose)
    from .engine.backtest import run_backtest

    cfg = _load(config, symbols, set_)
    summary = asyncio.run(run_backtest(cfg, from_, to, name, queue_usd, funding, db))
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


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


if __name__ == "__main__":
    app()
