@echo off
rem ---------------------------------------------------------------------------
rem hope: web monitor (reads the database only, never touches the engine).
rem Usage:  windows\monitor.cmd [--db data\hope.db,data\bt.db] [--port 8001]
rem Defaults come from .env: MONITOR_DB, MONITOR_PORT, MONITOR_HOST.
rem MONITOR_HOST=127.0.0.1 - this PC only; 0.0.0.0 - whole local network.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
title hope monitor
set "RC=0"
call "%~dp0_env.cmd" || goto :end
if not exist "%HOPE_EXE%" goto :not_installed
set "OPEN=--open"
if defined HOPE_NO_BROWSER set "OPEN="
if defined CI set "OPEN="
echo [hope] monitor: http://127.0.0.1:%MONITOR_PORT%/   databases: %MONITOR_DB%
"%HOPE_EXE%" monitor --db "%MONITOR_DB%" --host %MONITOR_HOST% --port %MONITOR_PORT% %OPEN% %*
set "RC=%ERRORLEVEL%"
goto :end

:not_installed
echo [hope] hope is not installed yet - run windows\install.cmd first.
set "RC=1"
:end
if not defined HOPE_NO_PAUSE if not defined CI pause
endlocal & exit /b %RC%
