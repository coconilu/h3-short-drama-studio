param(
    [string]$ApiBase = 'http://127.0.0.1:8765',
    [string]$OutputName = 'rain-call-ep01-roughcut-v1',
    [string]$OutputRoot = '',
    [string]$SourceSnapshotPath = '',
    [int]$Width = 1344,
    [int]$Height = 768,
    [string[]]$SkipSubtitleShotIds = @(),
    [hashtable]$SubtitleStartOffsets = @{},
    [switch]$RequireSelectedSources,
    [switch]$PolishAudio,
    [double]$DialogueTargetLufs = -18.0,
    [double]$AmbientTargetLufs = -24.0,
    [double]$RainBedVolume = 0.5
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ExportRoot = if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    Join-Path $ProjectRoot 'runtime\exports'
}
else {
    [IO.Path]::GetFullPath($OutputRoot)
}
New-Item -ItemType Directory -Path $ExportRoot -Force | Out-Null

function Get-MediaProbe([string]$Path) {
    $json = & ffprobe -v error -show_entries 'format=duration:stream=index,codec_type,width,height,r_frame_rate' -of json -- $Path
    if ($LASTEXITCODE -ne 0) {
        throw "ffprobe failed for $Path"
    }
    return $json | ConvertFrom-Json
}

function Get-JsonUtf8([string]$Uri) {
    $client = [Net.WebClient]::new()
    try {
        $bytes = $client.DownloadData($Uri)
        return [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
    }
    finally {
        $client.Dispose()
    }
}

function Format-SrtTime([double]$Seconds) {
    $span = [TimeSpan]::FromSeconds([Math]::Max(0.0, $Seconds))
    return '{0:00}:{1:00}:{2:00},{3:000}' -f [Math]::Floor($span.TotalHours), $span.Minutes, $span.Seconds, $span.Milliseconds
}

if (-not [string]::IsNullOrWhiteSpace($SourceSnapshotPath)) {
    if (-not (Test-Path -LiteralPath $SourceSnapshotPath -PathType Leaf)) {
        throw "Source snapshot does not exist: $SourceSnapshotPath"
    }
    $snapshot = Get-Content -LiteralPath $SourceSnapshotPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $project = [pscustomobject]@{ title = $snapshot.project_title }
    $shots = @($snapshot.sources | Sort-Object ordinal)
}
else {
    $project = Get-JsonUtf8 "$ApiBase/api/project"
    $shots = @($project.shots | Sort-Object ordinal)
}
if ($shots.Count -eq 0) {
    throw 'No shots are available for export.'
}

$sources = @()
foreach ($shot in $shots) {
    $shotId = if (-not [string]::IsNullOrWhiteSpace($SourceSnapshotPath)) { [string]$shot.shot_id } else { [string]$shot.id }
    $source = $null
    $sourceType = $null
    $sourceId = $null
    $sourceDetail = $null

    if (-not [string]::IsNullOrWhiteSpace($SourceSnapshotPath)) {
        $source = [string]$shot.path
        $sourceType = [string]$shot.source_type
        $sourceId = [string]$shot.source_id
        $sourceDetail = [string]$shot.source_detail
    }
    else {
        $hasLocalFinal = (
            $shot.final_output -and
            $shot.final_output.status -eq 'completed' -and
            -not [string]::IsNullOrWhiteSpace([string]$shot.final_output.output_file) -and
            (Test-Path -LiteralPath ([string]$shot.final_output.output_file) -PathType Leaf)
        )
        if ($hasLocalFinal) {
            $source = [string]$shot.final_output.output_file
            $sourceType = 'promotion'
            $sourceId = $shot.final_output.id
            $sourceDetail = $shot.final_output.strategy
        }
        else {
            $candidatePayload = Get-JsonUtf8 "$ApiBase/api/shots/$shotId/candidates"
            $candidates = [System.Collections.Generic.List[object]]::new()
            foreach ($candidate in $candidatePayload) {
                $candidates.Add($candidate)
            }
            $sourceCandidate = $candidates |
                Where-Object { $_.status -eq 'completed' -and $_.selected -eq 1 } |
                Select-Object -First 1
            if (-not $sourceCandidate -and -not $RequireSelectedSources) {
                $sourceCandidate = $candidates |
                    Where-Object { $_.status -eq 'completed' } |
                    Sort-Object created_at -Descending |
                    Select-Object -First 1
            }
            if ($sourceCandidate) {
                $source = [string]$sourceCandidate.output_file
                $sourceType = 'candidate'
                $sourceId = $sourceCandidate.id
                $sourceDetail = $sourceCandidate.source
            }
        }
    }

    if ([string]::IsNullOrWhiteSpace([string]$source) -or -not (Test-Path -LiteralPath ([string]$source) -PathType Leaf)) {
        throw "Shot $shotId has no completed local video source."
    }
    if (-not [string]::IsNullOrWhiteSpace($SourceSnapshotPath)) {
        if ([string]::IsNullOrWhiteSpace([string]$shot.checksum_sha256)) {
            throw "Shot $shotId has no frozen SHA256 credential."
        }
        $actualHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne ([string]$shot.checksum_sha256).ToLowerInvariant()) {
            throw "Shot $shotId no longer matches its frozen SHA256 credential."
        }
    }

    $probe = Get-MediaProbe $source
    $video = $probe.streams | Where-Object codec_type -eq 'video' | Select-Object -First 1
    $audio = $probe.streams | Where-Object codec_type -eq 'audio' | Select-Object -First 1
    if (-not $video -or -not $audio) {
        throw "Shot $shotId must contain both video and audio for this rough-cut export."
    }
    $mediaDuration = [double]$probe.format.duration
    $inPoint = if ($null -eq $shot.in_point_seconds) { 0.0 } else { [double]$shot.in_point_seconds }
    $outPoint = if ($null -eq $shot.out_point_seconds) { $mediaDuration } else { [double]$shot.out_point_seconds }
    if ($inPoint -lt 0 -or $outPoint -le $inPoint -or $outPoint -gt ($mediaDuration + 0.001)) {
        throw "Shot $shotId has invalid frozen in/out points."
    }
    $dialogueMode = if ([string]::IsNullOrWhiteSpace([string]$shot.dialogue_mode)) { 'original' } else { [string]$shot.dialogue_mode }

    $subtitleStartOffset = $null
    if ($SubtitleStartOffsets.ContainsKey($shotId)) {
        $subtitleStartOffset = [double]$SubtitleStartOffsets[$shotId]
    }
    elseif ($null -ne $shot.subtitle_start_seconds) {
        $subtitleStartOffset = [double]$shot.subtitle_start_seconds
    }
    $audioTargetLufs = $null
    if ($PolishAudio) {
        $audioTargetLufs = if ([string]::IsNullOrWhiteSpace($shot.dialogue)) {
            $AmbientTargetLufs
        }
        else {
            $DialogueTargetLufs
        }
    }

    $sources += [pscustomobject]@{
        shot_id = $shotId
        ordinal = $shot.ordinal
        title = $shot.title
        dialogue = $shot.dialogue
        source_type = $sourceType
        source_id = $sourceId
        source_detail = $sourceDetail
        path = $source
        duration_seconds = [Math]::Round($outPoint - $inPoint, 3)
        in_point_seconds = $inPoint
        out_point_seconds = $outPoint
        dialogue_mode = $dialogueMode
        checksum_sha256 = [string]$shot.checksum_sha256
        width = $video.width
        height = $video.height
        subtitle_enabled = if ($null -eq $shot.subtitle_enabled) { $true } else { [bool]$shot.subtitle_enabled }
        subtitle_start_seconds = $subtitleStartOffset
        audio_target_lufs = $audioTargetLufs
    }
}

$srtPath = Join-Path $ExportRoot "$OutputName.srt"
$srt = [System.Collections.Generic.List[string]]::new()
$cursor = 0.0
$subtitleIndex = 1
foreach ($source in $sources) {
    if (
        -not [string]::IsNullOrWhiteSpace($source.dialogue) -and
        $source.subtitle_enabled -and
        $SkipSubtitleShotIds -notcontains $source.shot_id
    ) {
        $localSubtitleStart = if ($null -ne $source.subtitle_start_seconds) {
            [double]$source.subtitle_start_seconds
        }
        else {
            [Math]::Min(0.8, $source.duration_seconds * 0.15)
        }
        $localSubtitleStart = [Math]::Max(0.0, [Math]::Min($localSubtitleStart, $source.duration_seconds - 0.3))
        $subtitleStart = $cursor + $localSubtitleStart
        $subtitleEnd = $cursor + [Math]::Min($source.duration_seconds - 0.15, $localSubtitleStart + 1.8)
        $srt.Add([string]$subtitleIndex)
        $srt.Add("$(Format-SrtTime $subtitleStart) --> $(Format-SrtTime $subtitleEnd)")
        $srt.Add($source.dialogue.Trim())
        $srt.Add('')
        $subtitleIndex += 1
    }
    $cursor += $source.duration_seconds
}
[IO.File]::WriteAllLines($srtPath, $srt, [Text.UTF8Encoding]::new($true))
$vttPath = Join-Path $ExportRoot "$OutputName.vtt"
$vttLines = [System.Collections.Generic.List[string]]::new()
$vttLines.Add('WEBVTT')
$vttLines.Add('')
foreach ($line in $srt) {
    $vttLines.Add(($line -replace '(\d{2}:\d{2}:\d{2}),(\d{3})', '$1.$2'))
}
[IO.File]::WriteAllLines($vttPath, $vttLines, [Text.UTF8Encoding]::new($false))

$sourceManifest = Join-Path $ExportRoot "$OutputName.sources.json"
[IO.File]::WriteAllText(
    $sourceManifest,
    ($sources | ConvertTo-Json -Depth 6),
    [Text.UTF8Encoding]::new($false)
)

$filterParts = [System.Collections.Generic.List[string]]::new()
$concatInputs = [System.Collections.Generic.List[string]]::new()
$totalDuration = ($sources | Measure-Object -Property duration_seconds -Sum).Sum
for ($index = 0; $index -lt $sources.Count; $index += 1) {
    $inPointText = ([double]$sources[$index].in_point_seconds).ToString('0.000', [Globalization.CultureInfo]::InvariantCulture)
    $outPointText = ([double]$sources[$index].out_point_seconds).ToString('0.000', [Globalization.CultureInfo]::InvariantCulture)
    $filterParts.Add("[$index`:v:0]trim=start=$inPointText`:end=$outPointText,setpts=PTS-STARTPTS,scale=$Width`:$Height`:force_original_aspect_ratio=decrease:flags=lanczos,pad=$Width`:$Height`:(ow-iw)/2`:(oh-ih)/2`:color=black,fps=24,format=yuv420p,setsar=1[v$index]")
    $audioFilter = "atrim=start=$inPointText`:end=$outPointText,asetpts=PTS-STARTPTS,aresample=48000`:async=1`:first_pts=0,aformat=sample_fmts=fltp`:sample_rates=48000`:channel_layouts=stereo"
    if ($sources[$index].dialogue_mode -eq 'mute') {
        $audioFilter += ',volume=0'
    }
    if ($PolishAudio) {
        $targetLufs = [double]$sources[$index].audio_target_lufs
        $targetLufsText = $targetLufs.ToString('0.0', [Globalization.CultureInfo]::InvariantCulture)
        $sourceDurationText = ([double]$sources[$index].duration_seconds).ToString('0.000', [Globalization.CultureInfo]::InvariantCulture)
        $audioFilter += ",loudnorm=I=$targetLufsText`:TP=-1.5`:LRA=11,aresample=48000,aformat=sample_fmts=fltp`:sample_rates=48000`:channel_layouts=stereo,atrim=duration=$sourceDurationText,asetpts=PTS-STARTPTS"
    }
    $filterParts.Add("[$index`:a:0]$audioFilter[a$index]")
    $concatInputs.Add("[v$index][a$index]")
}
if ($PolishAudio) {
    $filterParts.Add("$($concatInputs -join '')concat=n=$($sources.Count)`:v=1`:a=1[vout][amain]")
    $totalDurationText = ([double]$totalDuration).ToString('0.000', [Globalization.CultureInfo]::InvariantCulture)
    $rainBedVolumeText = $RainBedVolume.ToString('0.000', [Globalization.CultureInfo]::InvariantCulture)
    $filterParts.Add("[0`:a:0]atrim=start=0.2`:end=2.7,aresample=48000,aformat=sample_fmts=fltp`:sample_rates=48000`:channel_layouts=stereo,asetpts=PTS-STARTPTS,volume=$rainBedVolumeText,aloop=loop=-1`:size=120000,atrim=duration=$totalDurationText[bed]")
    $filterParts.Add('[amain][bed]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[aout]')
}
else {
    $filterParts.Add("$($concatInputs -join '')concat=n=$($sources.Count)`:v=1`:a=1[vout][aout]")
}
$filterGraph = $filterParts -join ';'

$outputPath = Join-Path $ExportRoot "$OutputName.mp4"
$arguments = [System.Collections.Generic.List[string]]::new()
$arguments.AddRange([string[]]@('-hide_banner', '-loglevel', 'warning', '-y'))
foreach ($source in $sources) {
    $arguments.Add('-i')
    $arguments.Add($source.path)
}
$arguments.Add('-i')
$arguments.Add($srtPath)
$arguments.AddRange([string[]]@(
    '-filter_complex', $filterGraph,
    '-map', '[vout]', '-map', '[aout]', '-map', "$($sources.Count):0",
    '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-r', '24', '-fps_mode', 'cfr',
    '-c:a', 'aac', '-b:a', '192k',
    '-c:s', 'mov_text', '-disposition:s:0', 'default',
    '-metadata', "title=$($project.title) · EP01 粗剪",
    '-movflags', '+faststart',
    $outputPath
))

& ffmpeg @arguments
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
    throw 'FFmpeg rough-cut export failed.'
}

$outputProbe = Get-MediaProbe $outputPath
[pscustomobject]@{
    output = $outputPath
    subtitles = $srtPath
    webvtt = $vttPath
    sources = $sourceManifest
    duration_seconds = [Math]::Round([double]$outputProbe.format.duration, 3)
    width = $Width
    height = $Height
    shot_count = $sources.Count
    audio_polish = [bool]$PolishAudio
    note = 'Low-resolution drafts are deterministically scaled to the output canvas; this does not add generated detail.'
}
