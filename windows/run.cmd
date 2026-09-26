@echo off
rem ---------------------------------------------------------------------------
rem hope: live paper engine only (restarts itself after crashes and network
rem failures). The monitor is windows\monitor.cmd; both at once: start.cmd.
rem Usage:  windows\run.cmd [hope run options]
rem   no options          strategy from STRATEGY_CONFIG in .env
rem   -c path\config.yaml another strategy config
rem   --duration 3600     stop after N seconds
rem   --set strategy.params.min_spread_bps=8   override any config key
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
title hope engine
set "RC=0"
call "%~dp0_env.cmd" || goto :end
if not exist "%HOPE_EXE%" goto :not_installed
echo [hope] engine: default strategy config %STRATEGY_CONFIG%, stop with Ctrl+C
"%HOPE_EXE%" run --restart %*
set "RC=%ERRORLEVEL%"
goto :end

:not_installed
echo [hope] hope is not installed yet - run windows\install.cmd first.
set "RC=1"
:end
if not defined HOPE_NO_PAUSE if not defined CI pause
endlocal & exit /b %RC%
