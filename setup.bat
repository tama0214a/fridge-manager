@echo off
rem ============================================================
rem  Fridge Manager - first-time setup (run once)
rem  Creates a Python virtual environment and installs packages.
rem ============================================================
cd /d "%~dp0"
echo === Fridge Manager setup ===

set "PY="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python --version >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo.
  echo [ERROR] Python not found.
  echo         Install Python 3.10 or newer from https://www.python.org/downloads/
  echo         then run this file again. See README.md for details.
  pause
  exit /b 1
)

echo Using: %PY%
%PY% -m venv .venv
if errorlevel 1 (
  echo [ERROR] Failed to create the virtual environment.
  pause
  exit /b 1
)

.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [ERROR] Package install failed. Check the network / proxy and retry.
  pause
  exit /b 1
)

echo.
echo Setup complete. Double-click start.bat to launch the app.
pause
