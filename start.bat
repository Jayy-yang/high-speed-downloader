@echo off
setlocal
cd /d "%~dp0"

rem Optional local dependency folder (offline / sandbox use)
if exist "%~dp0.deps" set "PYTHONPATH=%~dp0.deps"
if not defined PYTHONPATH if exist "%~dp0..\.deps" set "PYTHONPATH=%~dp0..\.deps"
if not defined PYTHONPATH if exist "%~dp0..\..\.deps" set "PYTHONPATH=%~dp0..\..\.deps"
set "PYTHONUTF8=1"
set "TARGET=%~dp0high_speed_downloader.py"

rem Prefer the py launcher / pythonw so no console window appears
where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3 "%TARGET%"
    exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%TARGET%"
    exit /b 0
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%TARGET%"
    exit /b %errorlevel%
)

echo Python 3.9+ not found. Install Python and enable "Add python.exe to PATH".
pause
exit /b 1
