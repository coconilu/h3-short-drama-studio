param(
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Supervisor = Join-Path $PSScriptRoot 'studio_supervisor.py'
$StatusFile = Join-Path $ProjectRoot 'runtime\supervisor-status.json'

if (-not (Test-Path -LiteralPath $BackendPython)) {
    throw 'Virtual environment is missing. Run scripts\setup.ps1 first.'
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'frontend\dist\index.html'))) {
    throw 'Frontend build is missing. Run scripts\setup.ps1 first.'
}

$AlreadyRunning = $false
if (Test-Path -LiteralPath $StatusFile) {
    try {
        $Status = Get-Content -LiteralPath $StatusFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $Age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [int64]$Status.updated_epoch
        $AlreadyRunning = $Age -lt 8 -and $null -ne (Get-Process -Id $Status.supervisor_pid -ErrorAction SilentlyContinue)
    } catch { $AlreadyRunning = $false }
}

if (-not $AlreadyRunning) {
    $Pythonw = Join-Path (Split-Path -Parent $BackendPython) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $Pythonw)) { $Pythonw = $BackendPython }
    Start-Process -FilePath $Pythonw -ArgumentList $Supervisor,'run' -WorkingDirectory $ProjectRoot -WindowStyle Hidden
}

$Deadline = [DateTime]::UtcNow.AddSeconds(30)
do {
    try {
        $null = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2
        $Web = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:4173/' -TimeoutSec 2
        if ($Web.StatusCode -eq 200) {
            Write-Host 'Jingchang is ready: http://127.0.0.1:4173/'
            if (-not $NoBrowser) {
                Start-Process 'http://127.0.0.1:4173/'
            }
            exit 0
        }
    } catch { Start-Sleep -Milliseconds 750 }
} while ([DateTime]::UtcNow -lt $Deadline)

throw "Jingchang was not ready within 30 seconds. Inspect runtime\supervisor-error.log, api.log and web.log."
