$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$FrontendRoot = Join-Path $ProjectRoot 'frontend'
$TargetRoot = Join-Path $FrontendRoot 'src-tauri\target\release'
$ReleaseRoot = Join-Path $ProjectRoot 'runtime\releases'

Push-Location $FrontendRoot
try {
    & pnpm 'desktop:bundle'
    if ($LASTEXITCODE -ne 0) { throw "Tauri build failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}

$DesktopExe = Join-Path $TargetRoot 'jingchang-desktop.exe'
$Installer = Get-ChildItem -LiteralPath (Join-Path $TargetRoot 'bundle\nsis') -Filter '*-setup.exe' |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not (Test-Path -LiteralPath $DesktopExe) -or $null -eq $Installer) {
    throw 'Tauri build completed without the expected EXE and NSIS installer.'
}

New-Item -ItemType Directory -Path $ReleaseRoot -Force | Out-Null
$ReleaseExe = Join-Path $ReleaseRoot 'jingchang-desktop.exe'
$ReleaseInstaller = Join-Path $ReleaseRoot $Installer.Name
Copy-Item -LiteralPath $DesktopExe -Destination $ReleaseExe -Force
Copy-Item -LiteralPath $Installer.FullName -Destination $ReleaseInstaller -Force

$Artifacts = @($ReleaseExe, $ReleaseInstaller) | ForEach-Object {
    $File = Get-Item -LiteralPath $_
    $Signature = Get-AuthenticodeSignature -LiteralPath $_
    [ordered]@{
        name = $File.Name
        path = $File.FullName
        size_bytes = $File.Length
        sha256 = (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant()
        signature = $Signature.Status.ToString()
    }
}
$Manifest = [ordered]@{
    schema_version = 1
    product = '镜场'
    version = '0.2.0'
    built_at = [DateTimeOffset]::UtcNow.ToString('o')
    workstation_bound = $true
    project_root = $ProjectRoot
    artifacts = $Artifacts
}
$Manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $ReleaseRoot 'desktop-release.json') -Encoding UTF8
Write-Host "Jingchang desktop release is ready: $ReleaseRoot"
