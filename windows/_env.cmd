@rem ---------------------------------------------------------------------------
@rem hope: internal helper for windows\*.cmd - do not run it directly.
@rem Goes to the repository root, creates .env from .env.example if needed,
@rem loads KEY=VALUE lines from .env (variables already set in the environment
@rem win), switches Python to UTF-8 and sets paths to the virtual environment.
@rem ---------------------------------------------------------------------------
@echo off
cd /d "%~dp0.." || exit /b 1
set "HOPE_ROOT=%CD%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist ".env" if exist ".env.example" copy /y ".env.example" ".env" >nul
if exist ".env" for /f "usebackq tokens=1,* delims==" %%A in (`findstr /r /b /c:"[A-Za-z_][A-Za-z0-9_]*=" ".env"`) do if not defined %%A set "%%A=%%B"
if not defined MONITOR_PORT set "MONITOR_PORT=8000"
if not defined MONITOR_HOST set "MONITOR_HOST=127.0.0.1"
if not defined MONITOR_DB set "MONITOR_DB=data/hope.db"
set "HOPE_EXE=%HOPE_ROOT%\.venv\Scripts\hope.exe"
set "HOPE_PY=%HOPE_ROOT%\.venv\Scripts\python.exe"
exit /b 0
