@echo off
rem ---------------------------------------------------------------------------
rem hope: start everything - the monitor in a separate minimized window (opens
rem the browser) and the live paper engine in this window.
rem Usage:  windows\start.cmd [hope run options]
rem   no options          strategy from STRATEGY_CONFIG in .env
rem   -c path\config.yaml another strategy config
rem   --duration 3600     stop after N seconds
rem Stop: Ctrl+C in this window, then close the "hope monitor" window.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
title hope engine
set "RC=0"
call "%~dp0_env.cmd" || goto :end
if not exist "%HOPE_EXE%" goto :not_installed

rem the monitor may already be running from a previous start.cmd - then just open it
"%HOPE_PY%" -c "import socket, sys; s = socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1', %MONITOR_PORT%)) == 0 else 1)"
if errorlevel 1 goto :start_monitor
echo [hope] monitor is already running on port %MONITOR_PORT% - opening it in the browser
if not defined HOPE_NO_BROWSER if not defined CI start "" "http://127.0.0.1:%MONITOR_PORT%/"
goto :monitor_ready
:start_monitor
start "hope monitor" /min cmd /c call "%~dp0monitor.cmd"
:monitor_ready

echo [hope] monitor: http://127.0.0.1:%MONITOR_PORT%/  - window "hope monitor"
echo [hope] engine: default strategy config %STRATEGY_CONFIG%, results in data\hope.db
echo [hope] stop the engine with Ctrl+C
echo.
"%HOPE_EXE%" run --restart %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="2" goto :bad_args
echo.
echo [hope] engine stopped with code %RC%. The monitor keeps running - close its window when done.
goto :end

:bad_args
echo.
echo [hope] The engine did not start: extra text or unknown options were passed, see "Error" above.
echo        Run the script without extra text:
echo.
echo     windows\start.cmd
echo.
echo        Engine options are listed by: .venv\Scripts\hope.exe run --help
goto :end

:not_installed
echo [hope] hope is not installed yet - run windows\install.cmd first.
set "RC=1"
:end
if not defined HOPE_NO_PAUSE if not defined CI pause
endlocal & exit /b %RC%
