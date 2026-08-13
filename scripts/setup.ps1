$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BundledRuntimeRoot = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies'
$BundledPython = Join-Path $BundledRuntimeRoot 'python\python.exe'
$BundledPnpm = Join-Path $BundledRuntimeRoot 'bin\fallback\pnpm.cmd'
$PythonCommand = if (Test-Path -LiteralPath $BundledPython) {
    $BundledPython
} else {
    (Get-Command python -ErrorAction Stop).Source
}
$PnpmCommand = if (Test-Path -LiteralPath $BundledPnpm) {
    $BundledPnpm
} else {
    (Get-Command pnpm -ErrorAction Stop).Source
}

& $PythonCommand -m venv (Join-Path $ProjectRoot '.venv')
& (Join-Path $ProjectRoot '.venv\Scripts\python.exe') -m pip install -r (Join-Path $ProjectRoot 'backend\requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
& $PnpmCommand install --dir (Join-Path $ProjectRoot 'frontend')
if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
& $PnpmCommand --dir (Join-Path $ProjectRoot 'frontend') build
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
Write-Host 'Setup and build completed. Run scripts\start.ps1 to launch.'
