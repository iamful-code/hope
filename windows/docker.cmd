@echo off
rem ---------------------------------------------------------------------------
rem hope in Docker Desktop (engine + monitor containers).
rem Usage:  windows\docker.cmd [up|down|logs|ps|restart|export-db]
rem   up         build and start in the background, open the monitor (default)
rem   down       stop and remove the containers (data stays in the volume)
rem   logs       follow the engine log (Ctrl+C to leave)
rem   export-db  copy the results database to data\hope-docker.db
rem Data lives in the Docker volume "hope-data" (HOPE_DATA in .env to change):
rem SQLite in WAL mode is unreliable on Windows folders mounted into Docker.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
set "RC=0"
call "%~dp0_env.cmd" || goto :end
where docker >nul 2>nul
if errorlevel 1 goto :no_docker
docker info >nul 2>nul
if errorlevel 1 goto :not_running
if not defined HOPE_DATA set "HOPE_DATA=hope-data"

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=up"
if /i "%ACTION%"=="up" goto :up
if /i "%ACTION%"=="down" goto :down
if /i "%ACTION%"=="logs" goto :logs
if /i "%ACTION%"=="ps" goto :ps
if /i "%ACTION%"=="restart" goto :restart
if /i "%ACTION%"=="export-db" goto :export
echo Usage: windows\docker.cmd [up^|down^|logs^|ps^|restart^|export-db]
set "RC=2"
goto :end

:up
echo [hope] docker compose up -d --build   strategy: %STRATEGY_CONFIG%   data: %HOPE_DATA%
docker compose up -d --build
if errorlevel 1 goto :failed
echo [hope] started. Monitor: http://127.0.0.1:%MONITOR_PORT%/   engine log: windows\docker.cmd logs
if not defined HOPE_NO_BROWSER if not defined CI start "" "http://127.0.0.1:%MONITOR_PORT%/"
goto :end
:down
docker compose down
goto :end
:logs
docker compose logs -f --tail 200 engine
goto :end
:ps
docker compose ps
goto :end
:restart
docker compose restart
goto :end
:export
if not exist "data" mkdir "data"
docker compose exec -T engine python -c "import sqlite3; s = sqlite3.connect('/app/data/hope.db'); d = sqlite3.connect('/app/data/export.db'); s.backup(d); d.close()"
if errorlevel 1 goto :failed
docker compose cp engine:/app/data/export.db data\hope-docker.db
if errorlevel 1 goto :failed
echo [hope] saved data\hope-docker.db - view it with: windows\monitor.cmd --db data\hope-docker.db
goto :end

:no_docker
echo [hope] docker was not found. Install Docker Desktop: https://docs.docker.com/desktop/setup/install/windows-install/
echo        or run hope without Docker: windows\install.cmd, then windows\start.cmd
set "RC=1"
goto :end
:not_running
echo [hope] Docker Desktop is not running - start it and wait until it says "Engine running".
set "RC=1"
goto :end
:failed
echo [hope] docker command failed - see the messages above.
set "RC=1"
:end
if not defined HOPE_NO_PAUSE if not defined CI pause
endlocal & exit /b %RC%
