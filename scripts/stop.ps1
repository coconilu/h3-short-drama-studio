$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeRoot = Join-Path $ProjectRoot 'runtime'
$StatusFile = Join-Path $RuntimeRoot 'supervisor-status.json'
$CommandFile = Join-Path $RuntimeRoot 'supervisor-command.json'

if (-not (Test-Path -LiteralPath $StatusFile)) {
    Write-Host 'Jingchang supervisor was not detected.'
    exit 0
}

$Status = Get-Content -LiteralPath $StatusFile -Raw -Encoding UTF8 | ConvertFrom-Json
$Age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [int64]$Status.updated_epoch
if ($Age -ge 8 -or $null -eq (Get-Process -Id $Status.supervisor_pid -ErrorAction SilentlyContinue)) {
    Write-Host 'Supervisor status is stale. No process was terminated.'
    exit 1
}

$Command = @{
    id = [guid]::NewGuid().ToString('N')
    action = 'stop'
    service = 'all'
    requested_at = [DateTimeOffset]::UtcNow.ToString('o')
} | ConvertTo-Json
$Command | Set-Content -LiteralPath $CommandFile -Encoding UTF8

$Deadline = [DateTime]::UtcNow.AddSeconds(15)
do {
    Start-Sleep -Milliseconds 500
    if ($null -eq (Get-Process -Id $Status.supervisor_pid -ErrorAction SilentlyContinue)) {
        Write-Host 'Jingchang services stopped safely.'
        exit 0
    }
} while ([DateTime]::UtcNow -lt $Deadline)

throw 'Supervisor did not stop within 15 seconds. Inspect runtime\supervisor-error.log.'
