@echo off
REM Double-click this to open HWE Runner - Scaled. No PowerShell, no quoting, no flags.
REM
REM It starts a small web server on this machine only (127.0.0.1) and opens your browser.
REM Close this window to stop the server. It is read-only over runs that already exist.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on the PATH.
  echo Install Python 3.11+ and try again, or run:  py hwe_scaled_ui.py
  pause
  exit /b 1
)
python hwe_scaled_ui.py
if errorlevel 1 pause
