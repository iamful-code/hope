"""Загрузка .env и выбор конфига стратегии по STRATEGY_CONFIG (то, на что опираются скрипты windows/*.cmd)."""

import os

from typer.testing import CliRunner

from hope.config import default_strategy_config, load_dotenv


def test_load_dotenv_parses_and_keeps_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "﻿# комментарий\nSTRATEGY_CONFIG=strategies/x/config.yaml\nMONITOR_PORT = 8001\n"
        'QUOTED="a b"\nexport EXPORTED=1\nEMPTY=\nKEEP=from_file\nbroken line\n',
        encoding="utf-8",
    )
    for k in ("STRATEGY_CONFIG", "MONITOR_PORT", "QUOTED", "EXPORTED", "EMPTY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KEEP", "from_env")
    applied = load_dotenv(env)
    assert os.environ["STRATEGY_CONFIG"] == "strategies/x/config.yaml"
    assert os.environ["MONITOR_PORT"] == "8001"
    assert os.environ["QUOTED"] == "a b"
    assert os.environ["EXPORTED"] == "1"
    assert os.environ["EMPTY"] == ""
    assert os.environ["KEEP"] == "from_env" and "KEEP" not in applied
    assert str(default_strategy_config()).replace("\\", "/") == "strategies/x/config.yaml"
    assert load_dotenv(tmp_path / "missing.env") == {}


def test_cli_uses_strategy_config_from_dotenv(tmp_path, monkeypatch):
    """`hope backtest` без -c берёт конфиг из STRATEGY_CONFIG в .env текущего каталога."""
    cfg = tmp_path / "strategies" / "demo" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("exchange:\n  symbols: []\n", encoding="utf-8")
    (tmp_path / ".env").write_text("STRATEGY_CONFIG=strategies/demo/config.yaml\n", encoding="utf-8")
    monkeypatch.delenv("STRATEGY_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    from hope.cli import app

    res = CliRunner().invoke(app, ["backtest", "--from", "2026-09-01", "--to", "2026-09-01"])
    # символы пусты -> бэктест должен отказаться именно с этой ошибкой, значит конфиг из .env подхватился
    assert res.exit_code != 0
    assert "exchange.symbols" in repr(res.exception)
    assert os.environ.get("STRATEGY_CONFIG") == "strategies/demo/config.yaml"
