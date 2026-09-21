@echo off
cd /d "%~dp0"
if not exist daily.local.json (
  copy daily.example.json daily.local.json >nul
  echo Please edit sender and recipient in daily.local.json first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m research_agent.daily configure
if errorlevel 1 goto done
".venv\Scripts\python.exe" -m research_agent.daily check --live
:done
pause
