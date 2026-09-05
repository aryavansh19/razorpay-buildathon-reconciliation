<#
.SYNOPSIS
    Screen-record the terminal demo with ffmpeg, producing silent footage.

.DESCRIPTION
    Records the desktop while `python -m recon.demo` runs, then writes an mp4 you
    can narrate over. It captures video only. There is no narration track, on
    purpose: the pitch video is where a reviewer hears you reason about your own
    work, and a synthesised voice reading a script removes exactly the signal the
    video exists to carry.

    Requires ffmpeg on PATH or at -FfmpegPath. Nothing else.

.EXAMPLE
    .\tools\capture.ps1
    .\tools\capture.ps1 -Beats 2,3,4 -Out media\retake.mp4
    .\tools\capture.ps1 -Region 1920x1080+0+0
#>
param(
    [string]$Out = 'media\demo-terminal.mp4',
    [string]$Beats = '',
    [int]$Countdown = 5,
    [double]$Pace = 1.0,
    [int]$Framerate = 25,
    [int]$Crf = 20,
    # Optional WxH+X+Y to capture part of the screen instead of the whole desktop.
    [string]$Region = '',
    [string]$FfmpegPath = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

# -- locate ffmpeg ----------------------------------------------------------
$ffmpeg = if ($FfmpegPath) { $FfmpegPath }
          elseif (Get-Command ffmpeg -ErrorAction SilentlyContinue) { 'ffmpeg' }
          elseif (Test-Path 'C:\ffmpeg\bin\ffmpeg.exe') { 'C:\ffmpeg\bin\ffmpeg.exe' }
          else { $null }

if (-not $ffmpeg) {
    Write-Error 'ffmpeg not found. Install it (winget install Gyan.FFmpeg) or pass -FfmpegPath.'
}

# -- locate python ----------------------------------------------------------
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { 'python' }

# -- prepare output ---------------------------------------------------------
$outPath = if ([System.IO.Path]::IsPathRooted($Out)) { $Out } else { Join-Path $root $Out }
$outDir = Split-Path -Parent $outPath
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }

# -- build the ffmpeg input -------------------------------------------------
$inputArgs = @('-f', 'gdigrab', '-framerate', "$Framerate")
if ($Region) {
    if ($Region -notmatch '^(\d+)x(\d+)\+(\d+)\+(\d+)$') {
        Write-Error "-Region must look like 1920x1080+0+0, got '$Region'"
    }
    $inputArgs += @(
        '-video_size', "$($Matches[1])x$($Matches[2])",
        '-offset_x', $Matches[3],
        '-offset_y', $Matches[4]
    )
}
$inputArgs += @('-i', 'desktop')

$encodeArgs = @(
    '-c:v', 'libx264', '-preset', 'veryfast', '-crf', "$Crf",
    # yuv420p keeps the file playable in browsers and by every reviewer's player.
    '-pix_fmt', 'yuv420p',
    # An even frame size is required by libx264 with yuv420p; odd desktop widths
    # otherwise fail after recording has already finished, which is the worst
    # possible time to find out.
    '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',
    '-movflags', '+faststart'
)

Write-Host ''
Write-Host '  Recording the terminal demo (video only, no audio track).' -ForegroundColor Cyan
Write-Host "  ffmpeg : $ffmpeg"
Write-Host "  output : $outPath"
Write-Host ''
Write-Host '  Before you start:' -ForegroundColor Yellow
Write-Host '   - Make the terminal large and increase the font size for legibility.'
Write-Host '   - Close notifications. A toast mid-take means a retake.'
Write-Host '   - Recording begins immediately; the demo waits for the countdown.'
Write-Host ''
Read-Host '  Press Enter when the terminal is ready'

# -- start recording --------------------------------------------------------
$ffmpegArgs = @('-y', '-loglevel', 'warning') + $inputArgs + $encodeArgs + @($outPath)
$recorder = Start-Process -FilePath $ffmpeg -ArgumentList $ffmpegArgs -PassThru -NoNewWindow

Start-Sleep -Milliseconds 800
if ($recorder.HasExited) {
    Write-Error "ffmpeg exited immediately (code $($recorder.ExitCode)). Check the gdigrab device is available."
}

try {
    $demoArgs = @('-m', 'recon.demo', '--countdown', "$Countdown", '--pace', "$Pace")
    if ($Beats) { $demoArgs += @('--beats', $Beats) }
    Push-Location $root
    & $python @demoArgs
    $demoExit = $LASTEXITCODE
    Pop-Location
}
finally {
    Start-Sleep -Seconds 2
    if (-not $recorder.HasExited) {
        # 'q' on stdin is the graceful stop, but Start-Process detaches stdin, so
        # CloseMainWindow then Kill is the reliable path. ffmpeg finalises the
        # moov atom on SIGTERM; +faststart above means a killed file still plays.
        $recorder.CloseMainWindow() | Out-Null
        Start-Sleep -Seconds 1
        if (-not $recorder.HasExited) { $recorder.Kill() }
    }
    $recorder.WaitForExit()
}

Write-Host ''
if (Test-Path $outPath) {
    $size = [math]::Round((Get-Item $outPath).Length / 1MB, 1)
    Write-Host "  Wrote $outPath ($size MB)" -ForegroundColor Green
    $probe = Get-Command ffprobe -ErrorAction SilentlyContinue
    if (-not $probe -and (Test-Path 'C:\ffmpeg\bin\ffprobe.exe')) { $probe = 'C:\ffmpeg\bin\ffprobe.exe' }
    if ($probe) {
        $exe = if ($probe -is [string]) { $probe } else { $probe.Source }
        $info = & $exe -v error -select_streams v:0 -show_entries stream=width,height,duration -of csv=p=0 $outPath
        Write-Host "  width,height,duration: $info"
    }
    Write-Host ''
    Write-Host '  Next: record your narration over this in any editor.' -ForegroundColor Yellow
    Write-Host '  The word-for-word script with timings is in VIDEO.md.'
} else {
    Write-Error 'No output file was produced.'
}
Write-Host ''

if ($demoExit -ne 0) { Write-Warning "The demo exited $demoExit; check the footage before using it." }
