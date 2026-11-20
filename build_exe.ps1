# Build a standalone HighSpeedDownloader.exe (no Python required on target machine).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File .\build_exe.ps1

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = 'C:\Users\Jayy_yang\AppData\Local\Programs\Python\Python314\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$env:PYTHONUTF8 = '1'

# aiohttp/requests live in the workspace .deps folder (site-packages is not writable here),
# so the analysis path must be passed explicitly or the frozen app fails with ModuleNotFoundError.
$deps = Join-Path $project '..\..\.deps'
if (Test-Path $deps) {
    $deps = (Resolve-Path $deps).Path
    $env:PYTHONPATH = $deps
    Write-Host "Using deps: $deps"
}

& $python -m PyInstaller `
    --noconfirm --clean --onefile --windowed --noupx `
    --name HighSpeedDownloader `
    --icon "$project\download-arrow.ico" `
    --version-file "$project\version_info.txt" `
    --paths $deps `
    --distpath "$project\dist" `
    --workpath "$project\build" `
    --specpath "$project" `
    "$project\high_speed_downloader.py"

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$exe = Join-Path $project 'dist\HighSpeedDownloader.exe'
Get-Item $exe | Select-Object FullName, Length, LastWriteTime
Write-Host "Done: $exe"
