# Build the portable package:
#   dist\portable\HighSpeedDownloader\   (onedir app + ed2k\Ed2kEngine.exe + docs)
#   dist\HighSpeedDownloader-<version>-portable.zip
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File .\build_portable.ps1
param(
    [string]$Python = '',
    [string]$Version = '1.1.1'
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
if (-not $Python) {
    $Python = 'C:\Users\Jayy_yang\AppData\Local\Programs\Python\Python314\python.exe'
    if (-not (Test-Path $Python)) {
        $found = Get-Command python -ErrorAction SilentlyContinue
        if ($found) { $Python = $found.Source } else { throw 'python not found' }
    }
}
$env:PYTHONUTF8 = '1'

# aiohttp/requests live in the workspace .deps folder (site-packages is not writable here),
# so the analysis path must be passed explicitly or the frozen app fails with ModuleNotFoundError.
$deps = Join-Path $root '..\..\.deps'
if (Test-Path $deps) {
    $deps = (Resolve-Path $deps).Path
    $env:PYTHONPATH = $deps
    Write-Host "Using deps: $deps"
}

$portableRoot = Join-Path $root 'dist\portable'
$appDir = Join-Path $portableRoot 'HighSpeedDownloader'
if (Test-Path $portableRoot) { Remove-Item $portableRoot -Recurse -Force }

Write-Host 'Building onedir application ...'
& $Python -m PyInstaller `
    --noconfirm --clean --onedir --windowed --noupx `
    --name HighSpeedDownloader `
    --icon (Join-Path $root 'download-arrow.ico') `
    --version-file (Join-Path $root 'version_info.txt') `
    --paths $deps `
    --distpath $portableRoot `
    --workpath (Join-Path $root 'build\portable') `
    --specpath (Join-Path $root 'build') `
    (Join-Path $root 'high_speed_downloader.py')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: $LASTEXITCODE" }

Write-Host 'Copying eD2K engine ...'
$engineDist = Join-Path $root 'ed2k-engine\dist\Ed2kEngine'
if (-not (Test-Path (Join-Path $engineDist 'Ed2kEngine.exe'))) {
    throw 'Ed2kEngine.exe not found - run build_engine.ps1 first'
}
$engineTarget = Join-Path $appDir 'ed2k'
New-Item -ItemType Directory -Force -Path $engineTarget | Out-Null
Copy-Item (Join-Path $engineDist '*') $engineTarget -Recurse -Force

Write-Host 'Copying documents ...'
foreach ($doc in 'README.md', 'LICENSE', 'LICENSE-GPL-3.0.txt', 'THIRD-PARTY-NOTICES.md') {
    $source = Join-Path $root $doc
    if (Test-Path $source) { Copy-Item $source $appDir -Force }
}
Copy-Item (Join-Path $root 'ed2k-engine\README.md') (Join-Path $appDir 'ED2K-README.md') -Force

$zip = Join-Path $root "dist\HighSpeedDownloader-$Version-portable.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Write-Host 'Creating zip ...'
Compress-Archive -Path $appDir -DestinationPath $zip -CompressionLevel Optimal
$size = [math]::Round((Get-Item $zip).Length / 1MB, 2)
Write-Host "Done: $zip ($size MB)"
