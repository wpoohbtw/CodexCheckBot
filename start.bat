@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv 2>nul
    if errorlevel 1 python -m venv .venv
    if errorlevel 1 goto :error
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :error

if not exist ".env" copy /Y ".env.example" ".env" >nul

".venv\Scripts\python.exe" -m codex_monitor %*
exit /b %errorlevel%

:error
echo Не удалось подготовить окружение Codex Limits Monitor.
exit /b 1
