@echo off
setlocal
set "BRIDGE_DIR=%~dp0"
set "BRIDGE_PYTHON=%BRIDGE_DIR%.venv\Scripts\pythonw.exe"

if not exist "%BRIDGE_PYTHON%" (
  echo Local Bridge chua duoc cai dat.
  echo Hay chay file install.ps1 truoc.
  pause
  exit /b 1
)

start "Asset Compensation Hub - Outlook Bridge" "%BRIDGE_PYTHON%" "%BRIDGE_DIR%app.py"
endlocal

