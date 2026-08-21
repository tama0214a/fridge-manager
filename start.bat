@echo off
rem ============================================================
rem  Fridge Manager - start the server (browser opens by itself)
rem  Keep this window open while using the app.
rem ============================================================
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Please run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe app.py
pause
