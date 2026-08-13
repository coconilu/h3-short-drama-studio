$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Candidates = @(
    (Join-Path $ProjectRoot 'runtime\releases\jingchang-desktop.exe'),
    (Join-Path $ProjectRoot 'frontend\src-tauri\target\release\jingchang-desktop.exe')
)
$DesktopExe = $Candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $DesktopExe) {
    throw 'Desktop executable is missing. Enter frontend and run: pnpm desktop:build'
}
Start-Process -FilePath $DesktopExe -WorkingDirectory $ProjectRoot
Write-Host 'Jingchang desktop client launched.'
