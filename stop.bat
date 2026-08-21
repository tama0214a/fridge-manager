@echo off
rem ============================================================
rem  Fridge Manager - stop a server started with start_hidden.vbs
rem ============================================================
cd /d "%~dp0"
if not exist "data\server.pid" (
  echo Fridge Manager does not seem to be running (no pid file).
  pause
  exit /b 0
)
set /p PID=<data\server.pid
taskkill /PID %PID% /F
if not errorlevel 1 del data\server.pid
echo Stopped.
pause
