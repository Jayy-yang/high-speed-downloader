# Temporary: open the settings dialog and capture exactly its rectangle.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32b {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
"@
[void][Win32b]::SetProcessDPIAware()

$python = 'C:\Users\Jayy_yang\AppData\Local\Programs\Python\Python314\pythonw.exe'
$root = Split-Path -Parent $PSScriptRoot
$deps = Join-Path $root '.deps'
if (-not (Test-Path $deps)) { $deps = Join-Path (Split-Path -Parent $root) '.deps' }
if (-not (Test-Path $deps)) { $deps = Join-Path (Split-Path -Parent (Split-Path -Parent $root)) '.deps' }
$env:PYTHONPATH = $deps
$env:PYTHONUTF8 = '1'

$rectFile = Join-Path $root 'tools\settings_rect.txt'
if (Test-Path $rectFile) { Remove-Item $rectFile -Force }

Get-Process python,pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
$proc = Start-Process -FilePath $python -ArgumentList 'tools\preview_settings.py' `
    -WorkingDirectory $root -PassThru
try {
    for ($i = 0; $i -lt 60 -and -not (Test-Path $rectFile); $i++) { Start-Sleep -Milliseconds 100 }
    if (-not (Test-Path $rectFile)) { throw 'settings dialog rect not reported' }
    Start-Sleep -Milliseconds 300
    $parts = (Get-Content $rectFile -Raw).Trim() -split '\s+'
    $x = [int]$parts[0]; $y = [int]$parts[1]; $w = [int]$parts[2]; $h = [int]$parts[3]
    Write-Output "dialog rect: $x,$y ${w}x${h}"

    $bitmap = New-Object System.Drawing.Bitmap $w, $h
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.CopyFromScreen($x, $y, 0, 0, $bitmap.Size)
    $out = Join-Path $root 'settings_preview.png'
    $bitmap.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose()
    $bitmap.Dispose()
    Write-Output "saved: $out"
}
finally {
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
    if (Test-Path $rectFile) { Remove-Item $rectFile -Force }
}
