@echo off
rem ---------------------------------------------------------------------------
rem hope: start the engine and the monitor automatically after you log in.
rem Usage:  windows\autostart.cmd on|off|status
rem Puts a small hope-lab.cmd into your Startup folder (no admin rights needed).
rem It runs windows\start.cmd minimized and does not open the browser.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
set "RC=0"
call "%~dp0_env.cmd" || goto :end
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LINK=%STARTUP%\hope-lab.cmd"
if /i "%~1"=="on" goto :on
if /i "%~1"=="off" goto :off
if exist "%LINK%" echo [hope] autostart is ON: %LINK%
if not exist "%LINK%" echo [hope] autostart is OFF
echo Usage: windows\autostart.cmd on^|off^|status
goto :end

:on
if not exist "%STARTUP%" mkdir "%STARTUP%"
> "%LINK%" echo @echo off
>> "%LINK%" echo rem hope lab autostart - created by windows\autostart.cmd, remove with: windows\autostart.cmd off
>> "%LINK%" echo set "HOPE_NO_BROWSER=1"
>> "%LINK%" echo start "hope lab" /min cmd /c call "%HOPE_ROOT%\windows\start.cmd"
if not exist "%LINK%" goto :failed
echo [hope] autostart ON: %LINK%
echo        After logon the engine and the monitor start minimized.
echo        Keep the PC awake: Settings - System - Power - Sleep: Never.
goto :end

:off
if exist "%LINK%" del /q "%LINK%"
echo [hope] autostart OFF
goto :end

:failed
echo [hope] could not write %LINK%
set "RC=1"
:end
if not defined HOPE_NO_PAUSE if not defined CI pause
endlocal & exit /b %RC%
