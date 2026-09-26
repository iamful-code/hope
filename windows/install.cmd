@echo off
rem ---------------------------------------------------------------------------
rem hope: install into .venv next to the repository (needs 64-bit Python 3.11+).
rem Usage:  windows\install.cmd [extras]
rem   extras - optional pip extras, e.g. freqtrade or dev. HOPE_EXTRAS from .env
rem            is added automatically (strategy/ft-* branches set it to freqtrade).
rem Run it again after "git pull" or after switching branches: it refreshes the installation.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
title hope - install
set "RC=0"
call "%~dp0_env.cmd" || goto :fail
echo [hope] repository: %HOPE_ROOT%

rem --- find Python: HOPE_PYTHON if set (e.g. py -3.11 or a full path to python.exe without spaces),
rem     then the py launcher (3.12, 3.11, 3.13, 3.14), then python on PATH
set "PYEXE="
if defined HOPE_PYTHON set "PYEXE=%HOPE_PYTHON%"
if defined PYEXE goto :have_python
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

rem Regular (not editable) install: "pip install -e" writes the source path into a .pth file
rem in UTF-8, while Python 3.11/3.12 on Windows read .pth files in the ANSI code page, so a
rem repository under a Cyrillic user folder becomes unimportable. The price: after
rem "git pull" run install.cmd again to refresh the installed copy of hope.
echo [hope] pip install "%SPEC%" - this takes a few minutes the first time ...
"%HOPE_PY%" -m pip install --disable-pip-version-check --upgrade pip
if errorlevel 1 goto :fail
"%HOPE_PY%" -m pip install --disable-pip-version-check "%SPEC%"
if errorlevel 1 goto :fail
rem same version number after "git pull" - force pip to replace hope itself (dependencies stay)
"%HOPE_PY%" -m pip install --disable-pip-version-check --no-deps --force-reinstall .
if errorlevel 1 goto :fail
"%HOPE_PY%" -c "import hope.cli"
if errorlevel 1 goto :fail
"%HOPE_EXE%" --help >nul
if errorlevel 1 goto :fail
if not exist "data" mkdir "data"

echo.
echo [hope] installed. Strategy config from .env: %STRATEGY_CONFIG%
echo.
echo [hope] Next step - start the engine and the monitor with this command:
echo.
echo     windows\start.cmd
echo.
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
