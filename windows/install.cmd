@echo off
rem ---------------------------------------------------------------------------
rem hope: install into .venv next to the repository (needs 64-bit Python 3.11+).
rem Usage:  windows\install.cmd [extras]
rem   extras - optional pip extras, e.g. freqtrade or dev. HOPE_EXTRAS from .env
rem            is added automatically (strategy/ft-* branches set it to freqtrade).
rem Safe to run again: updates the installation after "git pull".
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
title hope - install
set "RC=0"
call "%~dp0_env.cmd" || goto :fail
echo [hope] repository: %HOPE_ROOT%

rem --- find Python: py launcher first (3.12, 3.11, 3.13, 3.14), then python on PATH
set "PYEXE="
where py >nul 2>nul
if errorlevel 1 goto :try_python
for %%V in (3.12 3.11 3.13 3.14) do if not defined PYEXE py -%%V -c "import sys" >nul 2>nul && set "PYEXE=py -%%V"
:try_python
if defined PYEXE goto :have_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PYEXE=python"
if defined PYEXE goto :have_python
echo.
echo [hope] Python 3.11 or newer was not found.
echo        Install Python 3.12 from https://www.python.org/downloads/windows/
echo        and tick "Add python.exe to PATH", or run in a terminal:
echo            winget install -e --id Python.Python.3.12
echo        Then run windows\install.cmd again.
goto :fail

:have_python
echo [hope] using: %PYEXE%
%PYEXE% --version
%PYEXE% -c "import struct, sys; sys.exit(0 if struct.calcsize('P') == 8 else 1)"
if errorlevel 1 goto :py32

rem --- virtual environment
if exist ".venv\Scripts\python.exe" goto :venv_ready
echo [hope] creating virtual environment .venv ...
%PYEXE% -m venv .venv
if errorlevel 1 goto :fail
:venv_ready

rem --- extras: HOPE_EXTRAS from .env plus the command line argument
set "EXTRAS=%HOPE_EXTRAS%"
if not "%~1"=="" if defined EXTRAS set "EXTRAS=%EXTRAS%,%~1"
if not "%~1"=="" if not defined EXTRAS set "EXTRAS=%~1"
set "SPEC=."
if defined EXTRAS set "SPEC=.[%EXTRAS%]"

echo [hope] pip install -e "%SPEC%" - this takes a few minutes the first time ...
"%HOPE_PY%" -m pip install --disable-pip-version-check --upgrade pip
if errorlevel 1 goto :fail
"%HOPE_PY%" -m pip install --disable-pip-version-check -e "%SPEC%"
if errorlevel 1 goto :fail
"%HOPE_EXE%" --help >nul
if errorlevel 1 goto :fail
if not exist "data" mkdir "data"

echo.
echo [hope] installed.
echo        strategy config from .env: %STRATEGY_CONFIG%
echo        next step: windows\start.cmd  - engine + monitor in the browser
goto :end

:py32
echo [hope] 32-bit Python is not supported. Install 64-bit Python 3.12 and run this again.
:fail
echo.
echo [hope] INSTALL FAILED - see the messages above.
set "RC=1"
:end
if not defined HOPE_NO_PAUSE if not defined CI pause
endlocal & exit /b %RC%
