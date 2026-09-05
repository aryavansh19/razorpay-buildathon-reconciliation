<#
.SYNOPSIS
    Produce silent screen-recorded footage of the terminal walkthrough.

.DESCRIPTION
    Launches a dedicated console window at a legible font size, runs
    `python -m recon.demo` inside it, and records the desktop with ffmpeg while it
    runs. Output is video only, with no audio track, ready for a voiceover.

    The console is launched through conhost.exe explicitly rather than whatever the
    default terminal happens to be, because the font size is set through
    SetCurrentConsoleFontEx and that only applies to the classic console host. On a
    machine where Windows Terminal is the default, letting the OS choose would
    silently produce unreadably small text and the problem would only be visible
    after the take.

    ffmpeg is stopped by writing "q" to its stdin rather than by killing it, so the
    container is finalised properly and the file is seekable.

.EXAMPLE
    .\tools\record_walkthrough.ps1
    .\tools\record_walkthrough.ps1 -Beats 5 -Out media\beat5.mp4
    .\tools\record_walkthrough.ps1 -Pace 1.8 -FontHeight 22
#>
param(
    [string]$Out = 'media\walkthrough.mp4',
    [string]$Beats = '',
    [double]$Pace = 1.0,
    [int]$Framerate = 24,
    [int]$Crf = 21,
    # The demo formats to 74 columns. A snug window at a large font fills the frame
    # with text; maximising instead leaves most of the width empty, because the
    # content does not get wider just because the window does.
    #
    # The row count is the real constraint. The longest beat needs 55 lines, so a
    # short window makes its opening lines scroll away before they can be read. 46
    # rows at 22pt is the compromise: still legible on a 1080p frame, and the worst
    # case only scrolls by single digits. The stage script clamps to whatever the
    # display can actually fit.
    [int16]$FontHeight = 22,
    [int]$Cols = 92,
    [int]$Rows = 46,
    [switch]$Maximise,
    # Seconds between the console appearing and ffmpeg starting to record.
    [int]$LeadIn = 3,
    # Seconds the demo waits before its first beat, so recording is already running.
    [int]$Countdown = 5,
    # Safety cap. Recording stops after this regardless.
    [int]$MaxSeconds = 900,
    # Capture just the console window rather than the whole desktop. Keeps other
    # windows, notifications and the taskbar out of the footage.
    [string]$WindowTitle = 'recon walkthrough',
    [switch]$FullDesktop,
    [string]$FfmpegPath = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

# -- locate tools -----------------------------------------------------------
$ffmpeg = if ($FfmpegPath) { $FfmpegPath }
          elseif (Get-Command ffmpeg -ErrorAction SilentlyContinue) { (Get-Command ffmpeg).Source }
          elseif (Test-Path 'C:\ffmpeg\bin\ffmpeg.exe') { 'C:\ffmpeg\bin\ffmpeg.exe' }
          else { $null }
if (-not $ffmpeg) { Write-Error 'ffmpeg not found. winget install Gyan.FFmpeg, or pass -FfmpegPath.' }

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { 'python' }

$outPath = if ([System.IO.Path]::IsPathRooted($Out)) { $Out } else { Join-Path $root $Out }
$outDir = Split-Path -Parent $outPath
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }
if (Test-Path $outPath) { Remove-Item $outPath -Force }

# -- staging script run inside the recorded console -------------------------
# The demo writes its own exact beat timings. Recovering them from the footage
# afterwards means guessing from pixel changes, which mislabels beats whose output is
# sparse. The demo already knows, so it records them.
$timelinePath = [System.IO.Path]::ChangeExtension($outPath, '.timeline.json')
$demoArgs = "--countdown $Countdown --pace $Pace --timeline '$timelinePath'"
if ($Beats) { $demoArgs += " --beats $Beats" }

$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("recon_stage_" + [guid]::NewGuid().ToString('N') + ".ps1")
$stageBody = @"
`$ErrorActionPreference = 'Continue'

# The window title is set first and on its own, because it is what ffmpeg keys on
# to capture this window rather than the whole desktop. Bundling it with the resize
# calls would mean a resize failure silently leaves the title unset, and the capture
# would then fall back to filming the entire screen including whatever else is open.
try { `$Host.UI.RawUI.WindowTitle = '$WindowTitle' } catch { }

`$src = @'
using System;
using System.Runtime.InteropServices;
public class ReconConsole {
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  public struct CONSOLE_FONT_INFO_EX {
    public uint cbSize;
    public uint nFont;
    public short FontWidth;
    public short FontHeight;
    public int FontFamily;
    public int FontWeight;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string FaceName;
  }
  [DllImport("kernel32.dll", SetLastError = true)]
  public static extern IntPtr GetStdHandle(int nStdHandle);
  [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
  public static extern bool SetCurrentConsoleFontEx(IntPtr h, bool max, ref CONSOLE_FONT_INFO_EX f);
  [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("kernel32.dll")] public static extern bool GetConsoleMode(IntPtr h, out uint mode);
  [DllImport("kernel32.dll")] public static extern bool SetConsoleMode(IntPtr h, uint mode);

  // QuickEdit is on by default in the classic console. A single click inside the
  // window puts it into selection mode, which blocks the running program the next
  // time it writes to stdout and renames the window to "Select <title>".
  //
  // Both consequences are fatal to a recording. The demo freezes with no error, and
  // ffmpeg loses the window it was capturing by title and starts failing every
  // frame. Turning QuickEdit off makes a stray click harmless.
  public static bool DisableQuickEdit() {
    IntPtr h = GetStdHandle(-10);
    uint mode;
    if (!GetConsoleMode(h, out mode)) { return false; }
    mode &= ~0x0040u;             // ENABLE_QUICK_EDIT_MODE off
    mode &= ~0x0010u;             // ENABLE_MOUSE_INPUT off
    mode |= 0x0080u;              // ENABLE_EXTENDED_FLAGS, required for it to apply
    return SetConsoleMode(h, mode);
  }

  public static bool SetFont(string face, short height) {
    var info = new CONSOLE_FONT_INFO_EX();
    info.cbSize = (uint)Marshal.SizeOf(typeof(CONSOLE_FONT_INFO_EX));
    info.nFont = 0;
    info.FontWidth = 0;
    info.FontHeight = height;
    info.FontFamily = 54;
    info.FontWeight = 400;
    info.FaceName = face;
    return SetCurrentConsoleFontEx(GetStdHandle(-11), false, ref info);
  }
  public static void Maximise() {
    IntPtr h = GetConsoleWindow();
    ShowWindow(h, 3);
    SetForegroundWindow(h);
  }
  public static void Foreground() {
    IntPtr h = GetConsoleWindow();
    ShowWindow(h, 5);
    SetForegroundWindow(h);
  }
}
'@
try { Add-Type -TypeDefinition `$src -ErrorAction Stop } catch { }
try { [void][ReconConsole]::DisableQuickEdit() } catch { }
try { [void][ReconConsole]::SetFont('Consolas', $FontHeight) } catch { }

try {
  `$raw = `$Host.UI.RawUI
  `$w = [Math]::Min($Cols, `$raw.MaxPhysicalWindowSize.Width)
  `$h = [Math]::Min($Rows, `$raw.MaxPhysicalWindowSize.Height)
  `$raw.BufferSize = New-Object Management.Automation.Host.Size(`$w, 4000)
  `$raw.WindowSize = New-Object Management.Automation.Host.Size(`$w, `$h)
} catch { }

# Brought to the foreground so nothing overlaps it. gdigrab reads the window
# surface, so an overlapping window would land in the footage.
try { [ReconConsole]::Foreground() } catch { }
$(if ($Maximise) { "try { [ReconConsole]::Maximise(); Start-Sleep -Milliseconds 400; `$r = `$Host.UI.RawUI; `$r.BufferSize = New-Object Management.Automation.Host.Size(`$r.WindowSize.Width, 4000) } catch { }" })

Clear-Host
Set-Location '$root'
& '$python' -m recon.demo $demoArgs
"@
Set-Content -Path $stage -Value $stageBody -Encoding UTF8

# -- launch the console -----------------------------------------------------
Write-Host ''
Write-Host '  Recording the walkthrough. Video only, no audio track.' -ForegroundColor Cyan
Write-Host "  ffmpeg  : $ffmpeg"
Write-Host "  output  : $outPath"
Write-Host "  pace    : $Pace   font: Consolas $FontHeight   console: ${Cols}x${Rows}"
Write-Host "  capture : $(if ($FullDesktop) { 'full desktop' } else { "window '$WindowTitle'" })"
Write-Host ''
Write-Host '  A maximised console will open and take the foreground. Leave it alone' -ForegroundColor Yellow
Write-Host '  until it finishes, and do not click into another window.' -ForegroundColor Yellow
Write-Host ''

$console = Start-Process -FilePath 'conhost.exe' `
    -ArgumentList @('powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $stage) `
    -PassThru

Start-Sleep -Seconds $LeadIn
if ($console.HasExited) {
    Remove-Item $stage -Force -ErrorAction SilentlyContinue
    Write-Error "The console exited before recording started (code $($console.ExitCode))."
}

# -- start ffmpeg with stdin so it can be stopped cleanly -------------------
$grabTarget = if ($FullDesktop) { 'desktop' } else { "title=$WindowTitle" }
$ffArgs = @(
    '-y', '-loglevel', 'error',
    '-f', 'gdigrab', '-framerate', "$Framerate",
    # The pointer is not part of the demo and a stray cursor sitting in frame for
    # four minutes is distracting.
    '-draw_mouse', '0',
    '-i', $grabTarget,
    '-t', "$MaxSeconds",
    '-c:v', 'libx264', '-preset', 'veryfast', '-crf', "$Crf",
    '-pix_fmt', 'yuv420p',
    # Letterbox the console window into a 1920x1080 frame rather than scaling it.
    # Padding keeps the glyphs pixel-exact; upscaling a terminal blurs the text,
    # which is the one thing this footage exists to show. The pad colour matches the
    # console background so the bars are invisible. The scale step only ever shrinks,
    # as a guard for a window larger than 1080p.
    '-vf', ("scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease," +
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0C0C0C"),
    $outPath
)

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $ffmpeg
$psi.Arguments = ($ffArgs | ForEach-Object { if ($_ -match '[\s]') { '"' + $_ + '"' } else { $_ } }) -join ' '
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.CreateNoWindow = $true
$recorder = [System.Diagnostics.Process]::Start($psi)

Start-Sleep -Milliseconds 900
if ($recorder.HasExited) {
    if (-not $console.HasExited) { $console.Kill() }
    Remove-Item $stage -Force -ErrorAction SilentlyContinue
    Write-Error "ffmpeg exited immediately (code $($recorder.ExitCode))."
}

Write-Host '  Recording...' -ForegroundColor Green
$sw = [Diagnostics.Stopwatch]::StartNew()

# -- wait for the demo to finish, watching for a stall ----------------------
# The watchdog exists because the failure mode it catches is silent. If the console
# ends up in selection mode the demo blocks forever and ffmpeg keeps writing frames
# of a window it can no longer find, so without this the first sign of trouble is a
# recording that never ends.
$ticks = 0
$titleMissing = 0
$stalled = $false
while (-not $console.HasExited -and $sw.Elapsed.TotalSeconds -lt $MaxSeconds) {
    Start-Sleep -Milliseconds 500
    $ticks++
    if ($ticks % 10 -ne 0) { continue }

    $titles = @(Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle } | Select-Object -ExpandProperty MainWindowTitle)
    if ($titles -contains $WindowTitle) {
        $titleMissing = 0
        continue
    }

    $selecting = @($titles | Where-Object { $_ -like "*$WindowTitle*" })
    if ($selecting.Count -gt 0) {
        Write-Host ''
        Write-Host "  Stalled: the console is in selection mode ('$($selecting[0])')." -ForegroundColor Red
        Write-Host '  A click inside the window paused it. Aborting this take.' -ForegroundColor Red
        $stalled = $true
        break
    }

    $titleMissing++
    if ($titleMissing -ge 3) {
        Write-Host ''
        Write-Host "  Stalled: no window titled '$WindowTitle' for 15s. Aborting." -ForegroundColor Red
        $stalled = $true
        break
    }
}
# Let the closing card sit on screen for a beat before cutting.
Start-Sleep -Seconds 2
$sw.Stop()

# -- stop ffmpeg gracefully -------------------------------------------------
try {
    if (-not $recorder.HasExited) {
        $recorder.StandardInput.Write('q')
        $recorder.StandardInput.Flush()
        $recorder.StandardInput.Close()
    }
} catch { }
if (-not $recorder.WaitForExit(15000)) { $recorder.Kill(); $recorder.WaitForExit() }

if (-not $console.HasExited) { $console.Kill() }
Remove-Item $stage -Force -ErrorAction SilentlyContinue

# -- report -----------------------------------------------------------------
Write-Host ''
if (-not (Test-Path $outPath)) { Write-Error 'No output file was produced.' }

$sizeMb = [math]::Round((Get-Item $outPath).Length / 1MB, 1)
Write-Host "  Wrote $outPath ($sizeMb MB) in $([math]::Round($sw.Elapsed.TotalSeconds))s wall" -ForegroundColor Green
if ($stalled) {
    Write-Host '  This take is incomplete because the demo stalled. Re-run it.' -ForegroundColor Red
}

$ffprobe = if (Get-Command ffprobe -ErrorAction SilentlyContinue) { (Get-Command ffprobe).Source }
           elseif (Test-Path 'C:\ffmpeg\bin\ffprobe.exe') { 'C:\ffmpeg\bin\ffprobe.exe' }
           else { $null }
if ($ffprobe) {
    $info = & $ffprobe -v error -select_streams v:0 `
        -show_entries stream=width,height,nb_frames,duration,avg_frame_rate `
        -of default=nw=1 $outPath
    Write-Host '  ffprobe:'
    $info -split "`n" | Where-Object { $_.Trim() } | ForEach-Object { Write-Host "    $($_.Trim())" }
    $streams = & $ffprobe -v error -show_entries stream=codec_type -of csv=p=0 $outPath
    Write-Host "    streams: $(($streams -split "`n" | Where-Object { $_.Trim() }) -join ', ')"
}
if (Test-Path $timelinePath) {
    $marks = (Get-Content $timelinePath -Raw | ConvertFrom-Json).Count
    Write-Host "  timeline: $timelinePath ($marks marks)" -ForegroundColor Green
}
Write-Host ''
Write-Host '  No audio track, as intended. Narration script is in VIDEO.md.' -ForegroundColor Yellow
Write-Host ''
