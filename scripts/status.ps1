$ProjectRoot = Split-Path -Parent $PSScriptRoot
$StatusFile = Join-Path $ProjectRoot 'runtime\supervisor-status.json'
if (-not (Test-Path -LiteralPath $StatusFile)) {
    Write-Host 'Jingchang supervisor: not running'
    exit 1
}
$Status = Get-Content -LiteralPath $StatusFile -Raw -Encoding UTF8 | ConvertFrom-Json
$Age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [int64]$Status.updated_epoch
Write-Host "Supervisor PID: $($Status.supervisor_pid) | updated $([math]::Round($Age, 1)) seconds ago"
$Status.services.PSObject.Properties | ForEach-Object {
    $Service = $_.Value
    Write-Host "$($_.Name): $($Service.state) | PID $($Service.pid) | port $($Service.port) | restarts $($Service.restarts)"
}
if ($Age -ge 8) { exit 1 }
