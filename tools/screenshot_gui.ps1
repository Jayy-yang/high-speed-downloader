# 临时脚本：启动 GUI、截取窗口图像、关闭进程（仅开发期验证用）
param(
    [string]$Entry = 'high_speed_downloader.py',
    [string]$OutFile = 'gui_preview.png',
    [string]$ExePath = ''
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32 {
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int cmd);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindow(string cls, string title);
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
[void][Win32]::SetProcessDPIAware()

$python = 'C:\Users\Jayy_yang\AppData\Local\Programs\Python\Python314\pythonw.exe'
$root = Split-Path -Parent $PSScriptRoot
$deps = Join-Path $root '.deps'
if (-not (Test-Path $deps)) { $deps = Join-Path (Split-Path -Parent $root) '.deps' }
if (-not (Test-Path $deps)) { $deps = Join-Path (Split-Path -Parent (Split-Path -Parent $root)) '.deps' }
$env:PYTHONPATH = $deps
$env:PYTHONUTF8 = '1'

Get-Process python,pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
if ($ExePath) {
    $proc = Start-Process -FilePath $ExePath -WorkingDirectory (Split-Path -Parent $ExePath) -PassThru
} else {
    $proc = Start-Process -FilePath $python -ArgumentList $Entry `
        -WorkingDirectory $root -PassThru
}
try {
    Start-Sleep -Milliseconds 1500
    $handle = [IntPtr]::Zero
    for ($i = 0; $i -lt 40 -and $handle -eq [IntPtr]::Zero; $i++) {
        $proc.Refresh()
        $hwnd = $proc.MainWindowHandle
        if ($hwnd -and $hwnd -ne 0) { $handle = [IntPtr]$hwnd }
        if ($handle -eq [IntPtr]::Zero) { Start-Sleep -Milliseconds 250 }
    }
    if ($handle -eq [IntPtr]::Zero) {
        Write-Output "exited=$($proc.HasExited)"
        throw 'window handle not found'
    }
    [void][Win32]::ShowWindow($handle, 9)
    [void][Win32]::SetWindowPos($handle, [IntPtr](-1), 0, 0, 0, 0, 0x0003)
    [void][Win32]::SetForegroundWindow($handle)
    Start-Sleep -Milliseconds 500

    $rect = New-Object Win32+RECT
    [void][Win32]::GetWindowRect($handle, [ref]$rect)
    $width = $rect.Right - $rect.Left
    $height = $rect.Bottom - $rect.Top
    Write-Output "rect: $($rect.Left),$($rect.Top) ${width}x${height}"

    $bitmap = New-Object System.Drawing.Bitmap $width, $height
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size)
    $out = Join-Path $root $OutFile
    $bitmap.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
    $graphics.Dispose()
    $bitmap.Dispose()
    Write-Output "saved: $out"
}
finally {
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
}

