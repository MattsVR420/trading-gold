@echo off
setlocal enabledelayedexpansion

rem Volledige paden naar system-tools: werkt ook losgekoppeld en immuun voor PATH-hijacking
set "TASKLIST=%SystemRoot%\System32\tasklist.exe"
set "FIND=%SystemRoot%\System32\find.exe"
set "PING=%SystemRoot%\System32\PING.EXE"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

set "MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe"
set "PYTHON_PATH=C:\Users\matts\AppData\Local\Python\pythoncore-3.14-64\python.exe"
set "BOT_DIR=C:\Users\matts\Desktop\trading gold$"

rem -- MT5 draait? zo niet: starten en wachten tot ingelogd --------------
"%TASKLIST%" /FI "IMAGENAME eq terminal64.exe" | "%FIND%" /I "terminal64.exe" >NUL
if errorlevel 1 (
    echo MetaTrader 5 wordt gestart...
    start "" "%MT5_PATH%"
    call :wait_for_mt5
    echo MT5 gevonden - 25s wachten tot volledig ingelogd...
    call :sleep 25
) else (
    echo MetaTrader 5 draait al.
)

rem -- niet dubbel starten: draait strategy_b.py al? --------------------
"%PS%" -NoProfile -Command "exit ([bool](Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*strategy_b.py*' }))"
if errorlevel 1 (
    echo strategy_b.py draait al - niets te doen.
    goto :eof
)

echo strategy_b wordt gestart...
cd /d "%BOT_DIR%"
"%PYTHON_PATH%" strategy_b.py
goto :eof

rem -- subroutines ----------------------------------------------------
:wait_for_mt5
"%TASKLIST%" /FI "IMAGENAME eq terminal64.exe" | "%FIND%" /I "terminal64.exe" >NUL
if not errorlevel 1 goto :eof
call :sleep 5
goto wait_for_mt5

:sleep
rem %1 seconden pauze - werkt ook losgekoppeld (timeout /t doet dat niet)
set /a "_s=%1+1"
"%PING%" -n !_s! -w 1000 127.0.0.1 >NUL
goto :eof
