@echo off
rem ---------------------------------------------------------------------------
rem hope: backtest on the Bybit trade archive (public.bybit.com, no API keys).
rem Usage:  windows\backtest.cmd [hope backtest options]
rem   windows\backtest.cmd --from 2026-09-17 --to 2026-09-25 --mode candles --db data\bt.db
rem   windows\backtest.cmd -c strategies\queue_mm\config.yaml -s GRAMUSDT,ATOMUSDT --from 2026-09-23 --to 2026-09-25
rem Without -c the strategy comes from STRATEGY_CONFIG in .env. Results: open them in
rem the monitor, e.g. set MONITOR_DB=data/hope.db,data/bt.db in .env.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
title hope backtest
set "RC=0"
call "%~dp0_env.cmd" || goto :end
if not exist "%HOPE_EXE%" goto :not_installed
"%HOPE_EXE%" backtest %*
set "RC=%ERRORLEVEL%"
goto :end

:not_installed
echo [hope] hope is not installed yet - run windows\install.cmd first.
set "RC=1"
:end
if not defined HOPE_NO_PAUSE if not defined CI pause
endlocal & exit /b %RC%
