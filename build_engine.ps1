# Build the eD2K engine as a separate executable: ed2k-engine\dist\Ed2kEngine.exe
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File .\build_engine.ps1
param(
    [string]$Python = ''
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
$source = Join-Path $root 'ed2k-engine\engine.py'
$vendor = Join-Path $root 'ed2k-engine\vendor'
if (-not (Test-Path $source)) { throw "engine source not found: $source" }

# Console subsystem on purpose: the main program launches it with CREATE_NO_WINDOW,
# so stdin/stdout stay valid pipes (a --windowed build can end up with sys.stdout = None).
$arguments = @(
    '--noconfirm', '--clean', '--onedir', '--console', '--noupx',
    '--name', 'Ed2kEngine',
    '--distpath', (Join-Path $root 'ed2k-engine\dist'),
    '--workpath', (Join-Path $root 'ed2k-engine\build'),
    '--specpath', (Join-Path $root 'ed2k-engine'),
    '--paths', $vendor,
    '--hidden-import', 'Crypto.Hash.MD4',
    '--hidden-import', 'Crypto.Hash.SHA1',
    $source
)

Write-Host 'Building Ed2kEngine.exe ...'
& $Python -m PyInstaller @arguments
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$exe = Join-Path $root 'ed2k-engine\dist\Ed2kEngine\Ed2kEngine.exe'
if (-not (Test-Path $exe)) { throw "no output: $exe" }
$size = [math]::Round((Get-Item $exe).Length / 1MB, 2)
Write-Host "Done: $exe ($size MB)"

