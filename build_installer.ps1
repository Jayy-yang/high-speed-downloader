# Build the installer: dist\HighSpeedDownloader-Setup-<version>.exe
# Requires the portable folder from build_portable.ps1 (run it first).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File .\build_installer.ps1
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

$appDir = Join-Path $root 'dist\portable\HighSpeedDownloader'
if (-not (Test-Path (Join-Path $appDir 'HighSpeedDownloader.exe'))) {
    throw 'portable build not found - run build_portable.ps1 first'
}

$stage = Join-Path $root 'build\installer'
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

Write-Host 'Creating payload.zip ...'
$payload = Join-Path $stage 'payload.zip'
Compress-Archive -Path (Join-Path $appDir '*') -DestinationPath $payload -CompressionLevel Optimal
$size = [math]::Round((Get-Item $payload).Length / 1MB, 2)
Write-Host "payload: $payload ($size MB)"

$icon = Join-Path $root 'download-arrow.ico'
$versionFile = Join-Path $root 'build\installer-version_info.txt'
Copy-Item (Join-Path $root 'version_info.txt') $versionFile -Force
$text = Get-Content $versionFile -Raw
$text = $text -replace "OriginalFilename', 'HighSpeedDownloader.exe'", "OriginalFilename', 'HighSpeedDownloader-Setup.exe'"
$text = $text -replace "InternalName', 'HighSpeedDownloader'", "InternalName', 'HighSpeedDownloader-Setup'"
Set-Content -Path $versionFile -Value $text -Encoding utf8

Write-Host 'Building installer executable ...'
& $Python -m PyInstaller `
    --noconfirm --clean --onefile --windowed --noupx `
    --name HighSpeedDownloader-Setup-$Version `
    --icon $icon `
    --version-file $versionFile `
    --add-data "$payload;." `
    --distpath (Join-Path $root 'dist') `
    --workpath (Join-Path $root 'build\installer-work') `
    --specpath (Join-Path $root 'build') `
    (Join-Path $root 'installer\installer.py')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: $LASTEXITCODE" }

$exe = Join-Path $root "dist\HighSpeedDownloader-Setup-$Version.exe"
if (-not (Test-Path $exe)) { throw "no output: $exe" }
$exeSize = [math]::Round((Get-Item $exe).Length / 1MB, 2)
Write-Host "Done: $exe ($exeSize MB)"
