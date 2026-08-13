param(
    [int]$Port = 8765,
    [string]$Database = '',
    [string]$ExportRoot = '',
    [string]$ExportScript = '',
    [double]$ExportTestDelaySeconds = 0
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python runtime does not exist: $Python"
}

if (-not [string]::IsNullOrWhiteSpace($Database)) {
    $env:JINGCHANG_DB = [IO.Path]::GetFullPath($Database)
}
if (-not [string]::IsNullOrWhiteSpace($ExportRoot)) {
    $env:JINGCHANG_EXPORT_ROOT = [IO.Path]::GetFullPath($ExportRoot)
}
if (-not [string]::IsNullOrWhiteSpace($ExportScript)) {
    $env:JINGCHANG_EXPORT_SCRIPT = [IO.Path]::GetFullPath($ExportScript)
}
$env:JINGCHANG_API_BASE = "http://127.0.0.1:$Port"
$env:JINGCHANG_EXPORT_TEST_DELAY_SECONDS = $ExportTestDelaySeconds.ToString(
    [Globalization.CultureInfo]::InvariantCulture
)

& $Python -m uvicorn backend.app:app --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
