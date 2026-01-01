<#
.SYNOPSIS
    Optimizes screen capture videos by removing freeze frames and converting to GIF/WebP/MP4.

.DESCRIPTION
    This script analyzes video files to detect static/frozen segments using ffmpeg's freezedetect
    filter, removes those segments, and outputs an optimized animation in the desired format.
    
    Perfect for screen recordings where you want to remove pauses, loading screens, or idle time.

.PARAMETER InputFile
    Source video file path (required).

.PARAMETER FreezeMinSeconds
    Minimum freeze duration to cut (default: 7.0 seconds).

.PARAMETER NoiseThreshold
    Sensitivity for freeze detection. Options:
    - Not specified: Use ffmpeg default (~0.002)
    - "Auto": Auto-tune (try default, then 0.004 if no freezes found)
    - Numeric (0.0001-0.01): Manual threshold (higher = more tolerant of noise)
    - 0.0005 - Real world example normal screen recordings of a Linux terminal - only this small value works well along with 10s duration if more than that then cuts are too abrupt

.PARAMETER Fps
    Output frames per second (default: 5).

.PARAMETER Resolution
    Output resolution preset or custom width. Options:
    - 4K, 2160p: 3840px width
    - 2K, 1440p: 2560px width  
    - FHD, 1080p: 1920px width
    - HD, 720p: 1280px width
    - 480p: 854px width
    - 360p: 640px width
    - 240p: 426px width
    - Custom number (e.g., 800): exact width in pixels
    - Original (default): keep source resolution

.PARAMETER OutputFormat
    Output format: Gif, Webp, or Mp4 (default: Gif).

.PARAMETER OutputPath
    Custom output path. If not specified, creates {basename}_optimized.{ext} in same folder.

.PARAMETER FfmpegPath
    Path to ffmpeg/ffprobe binaries folder (default: script directory, then PATH).

.PARAMETER DebugFreezes
    Show detailed freeze detection information.

.EXAMPLE
    & .\screencapture-optimize.ps1 -InputFile "recording.mp4" -OutputFormat Gif
    
.EXAMPLE
    & .\screencapture-optimize.ps1 -InputFile "recording.mp4" -NoiseThreshold Auto -DebugFreezes

.NOTES
    HOW TO RUN:
    
    Option 1 - Run directly from GitHub (no download):
    irm https://raw.githubusercontent.com/MarvinFS/Public/main/screencapture-optimize/screencapture-optimize.ps1 | iex; screencapture-optimize -InputFile "video.mp4"
    
    Option 2 - Download and run locally:
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/MarvinFS/Public/main/screencapture-optimize/screencapture-optimize.ps1" -OutFile "screencapture-optimize.ps1"
    & .\screencapture-optimize.ps1 -InputFile "video.mp4"
    
    Option 3 - With execution policy bypass:
    powershell -ExecutionPolicy Bypass -File ".\screencapture-optimize.ps1" -InputFile "video.mp4"
    
    When running from PowerShell with a path containing spaces, you must use the & (call) operator:
    & "C:\path with spaces\screencapture-optimize.ps1" -InputFile "video.mp4"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [Alias("Input", "i")]
    [string]$InputFile,

    [Parameter()]
    [Alias("d")]
    [double]$FreezeMinSeconds = 7.0,

    [Parameter()]
    [Alias("n")]
    [string]$NoiseThreshold,

    [Parameter()]
    [int]$Fps = 5,

    [Parameter()]
    [Alias("res", "r")]
    [string]$Resolution = "Original",

    [Parameter()]
    [ValidateSet("Gif", "Webp", "Mp4")]
    [string]$OutputFormat = "Gif",

    [Parameter()]
    [Alias("o")]
    [string]$OutputPath,

    [Parameter()]
    [string]$FfmpegPath,

    [Parameter()]
    [switch]$DebugFreezes
)

#region ==================== CONFIGURATION ====================

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Colors for output
$Colors = @{
    Info    = 'Cyan'
    Success = 'Green'
    Warning = 'Yellow'
    Error   = 'Red'
    Debug   = 'DarkGray'
    Step    = 'White'
}

#endregion

#region ==================== HELPER FUNCTIONS ====================

function Write-Step {
    param([string]$Message)
    Write-Host "`n>>> $Message" -ForegroundColor $Colors.Step
}

function Write-Info {
    param([string]$Message)
    Write-Host "    $Message" -ForegroundColor $Colors.Info
}

function Write-Ok {
    param([string]$Message)
    Write-Host "    [OK] $Message" -ForegroundColor $Colors.Success
}

function Write-Warn {
    param([string]$Message)
    Write-Host "    [WARN] $Message" -ForegroundColor $Colors.Warning
}

function Write-Dbg {
    param([string]$Message)
    if ($DebugFreezes) {
        Write-Host "    [DBG] $Message" -ForegroundColor $Colors.Debug
    }
}

function Install-Ffmpeg {
    <#
    .SYNOPSIS
        Downloads and extracts ffmpeg to the script directory
    #>
    param([string]$DestinationPath)
    
    $downloadUrl = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    $zipPath = Join-Path $DestinationPath "ffmpeg-download.zip"
    
    Write-Host ""
    Write-Host "    ffmpeg not found!" -ForegroundColor $Colors.Warning
    Write-Host ""
    Write-Host "    Download options:" -ForegroundColor $Colors.Info
    Write-Host "      1. GitHub (BtbN):  https://github.com/BtbN/FFmpeg-Builds/releases" -ForegroundColor $Colors.Debug
    Write-Host "      2. Gyan.dev:       https://www.gyan.dev/ffmpeg/builds/" -ForegroundColor $Colors.Debug
    Write-Host "      3. Chocolatey:     choco install ffmpeg-full" -ForegroundColor $Colors.Debug
    Write-Host "      4. Scoop:          scoop install ffmpeg" -ForegroundColor $Colors.Debug
    Write-Host "      5. WinGet:         winget install ffmpeg" -ForegroundColor $Colors.Debug
    Write-Host ""
    
    $response = Read-Host "    Download automatically from GitHub? [Y/n]"
    if ($response -match '^[Nn]') {
        return $null
    }
    
    try {
        Write-Host ""
        Write-Host "    Downloading ffmpeg (~200MB)..." -ForegroundColor $Colors.Info
        Write-Host "    Source: $downloadUrl" -ForegroundColor $Colors.Debug
        
        # Download with progress
        $ProgressPreference = 'Continue'
        Invoke-WebRequest -Uri $downloadUrl -OutFile $zipPath -UseBasicParsing
        
        Write-Host "    Extracting..." -ForegroundColor $Colors.Info
        
        # Extract ZIP
        $extractPath = Join-Path $DestinationPath "ffmpeg-temp"
        Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force
        
        # Find the bin folder inside extracted archive (it's in a subdirectory)
        $binFolder = Get-ChildItem -Path $extractPath -Recurse -Directory -Filter "bin" | Select-Object -First 1
        
        if (-not $binFolder) {
            throw "Could not find bin folder in downloaded archive"
        }
        
        # Copy binaries to script directory
        Copy-Item -Path (Join-Path $binFolder.FullName "ffmpeg.exe") -Destination $DestinationPath -Force
        Copy-Item -Path (Join-Path $binFolder.FullName "ffprobe.exe") -Destination $DestinationPath -Force
        
        # Cleanup
        Remove-Item -Path $zipPath -Force -ErrorAction SilentlyContinue
        Remove-Item -Path $extractPath -Recurse -Force -ErrorAction SilentlyContinue
        
        Write-Host "    [OK] ffmpeg installed to: $DestinationPath" -ForegroundColor $Colors.Success
        
        return @{
            Ffmpeg  = Join-Path $DestinationPath "ffmpeg.exe"
            Ffprobe = Join-Path $DestinationPath "ffprobe.exe"
        }
    }
    catch {
        Write-Host "    [ERROR] Download failed: $_" -ForegroundColor $Colors.Error
        # Cleanup on failure
        Remove-Item -Path $zipPath -Force -ErrorAction SilentlyContinue
        Remove-Item -Path (Join-Path $DestinationPath "ffmpeg-temp") -Recurse -Force -ErrorAction SilentlyContinue
        return $null
    }
}

function Find-Ffmpeg {
    param([string]$CustomPath)
    
    $locations = @()
    
    # Custom path first
    if ($CustomPath -and (Test-Path $CustomPath)) {
        $locations += $CustomPath
    }
    
    # Script directory
    $scriptDir = Split-Path -Parent $PSCommandPath
    $locations += $scriptDir
    
    # Common locations
    $locations += @(
        "$env:LOCALAPPDATA\ffmpeg\bin",
        "$env:ProgramFiles\ffmpeg\bin"
    )
    
    foreach ($loc in $locations) {
        $ffmpeg = Join-Path $loc "ffmpeg.exe"
        $ffprobe = Join-Path $loc "ffprobe.exe"
        
        if ((Test-Path $ffmpeg) -and (Test-Path $ffprobe)) {
            return @{
                Ffmpeg  = $ffmpeg
                Ffprobe = $ffprobe
            }
        }
    }
    
    # Try PATH
    $ffmpegInPath = Get-Command "ffmpeg.exe" -ErrorAction SilentlyContinue
    $ffprobeInPath = Get-Command "ffprobe.exe" -ErrorAction SilentlyContinue
    
    if ($ffmpegInPath -and $ffprobeInPath) {
        return @{
            Ffmpeg  = $ffmpegInPath.Source
            Ffprobe = $ffprobeInPath.Source
        }
    }
    
    # Not found anywhere - offer to download
    $scriptDir = Split-Path -Parent $PSCommandPath
    $result = Install-Ffmpeg -DestinationPath $scriptDir
    
    if ($result) {
        return $result
    }
    
    throw "ffmpeg.exe and ffprobe.exe not found. Please install ffmpeg or specify -FfmpegPath."
}

function Get-VideoDuration {
    param([string]$VideoPath, [string]$FfprobePath)
    
    $output = & $FfprobePath -v error -show_entries format=duration -of csv=p=0 $VideoPath 2>&1
    
    if ($LASTEXITCODE -ne 0) {
        throw "ffprobe failed to get duration: $output"
    }
    
    $duration = [double]::Parse($output.Trim(), [System.Globalization.CultureInfo]::InvariantCulture)
    return $duration
}

function Get-VideoResolution {
    param([string]$VideoPath, [string]$FfprobePath)
    
    $output = & $FfprobePath -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 $VideoPath 2>&1
    
    if ($LASTEXITCODE -ne 0) {
        throw "ffprobe failed to get resolution: $output"
    }
    
    $parts = $output.Trim() -split ','
    return @{
        Width  = [int]$parts[0]
        Height = [int]$parts[1]
    }
}

function Invoke-FreezeDetect {
    param(
        [string]$VideoPath,
        [double]$MinDuration,
        [Nullable[double]]$Noise,
        [string]$FfmpegPath
    )
    
    # Build filter string
    $dParam = $MinDuration.ToString('0.##', [System.Globalization.CultureInfo]::InvariantCulture)
    
    if ($null -ne $Noise) {
        $nParam = $Noise.ToString('0.######', [System.Globalization.CultureInfo]::InvariantCulture)
        $filter = "freezedetect=n=${nParam}:d=${dParam}"
    }
    else {
        $filter = "freezedetect=d=${dParam}"
    }
    
    Write-Dbg "Filter: $filter"
    
    # Run ffmpeg freezedetect (output goes to stderr)
    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = $FfmpegPath
    $processInfo.Arguments = "-hide_banner -i `"$VideoPath`" -vf `"$filter`" -an -f null NUL"
    $processInfo.RedirectStandardError = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $processInfo
    $process.Start() | Out-Null
    
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    
    return $stderr
}

function ConvertTo-FreezeList {
    param([string]$FfmpegOutput)
    
    $freezes = [System.Collections.Generic.List[PSCustomObject]]::new()
    $lines = $FfmpegOutput -split "`n"
    
    $pendingStart = $null
    
    foreach ($line in $lines) {
        if ($line -match 'freeze_start:\s*([0-9.]+)') {
            $pendingStart = [double]::Parse($Matches[1], [System.Globalization.CultureInfo]::InvariantCulture)
        }
        elseif ($line -match 'freeze_end:\s*([0-9.]+)') {
            $end = [double]::Parse($Matches[1], [System.Globalization.CultureInfo]::InvariantCulture)
            if ($null -ne $pendingStart) {
                $freezes.Add([PSCustomObject]@{
                    Start    = $pendingStart
                    End      = $end
                    Duration = [math]::Round($end - $pendingStart, 3)
                })
                $pendingStart = $null
            }
        }
    }
    
    return $freezes
}

function Get-KeepSegments {
    param(
        [System.Collections.Generic.List[PSCustomObject]]$Freezes,
        [double]$TotalDuration,
        [double]$MinSegmentDuration = 0.5  # Minimum segment to keep (skip tiny fragments)
    )
    
    $segments = [System.Collections.Generic.List[PSCustomObject]]::new()
    $currentStart = 0.0
    
    foreach ($freeze in $Freezes) {
        if ($freeze.Start -gt $currentStart) {
            $segDuration = [math]::Round($freeze.Start - $currentStart, 3)
            # Only add segment if it's long enough to be meaningful
            if ($segDuration -ge $MinSegmentDuration) {
                $segments.Add([PSCustomObject]@{
                    Start    = $currentStart
                    End      = $freeze.Start
                    Duration = $segDuration
                })
            }
        }
        $currentStart = $freeze.End
    }
    
    # Add final segment if there's content after last freeze
    if ($currentStart -lt $TotalDuration) {
        $segDuration = [math]::Round($TotalDuration - $currentStart, 3)
        if ($segDuration -ge $MinSegmentDuration) {
            $segments.Add([PSCustomObject]@{
                Start    = $currentStart
                End      = $TotalDuration
                Duration = $segDuration
            })
        }
    }
    
    return $segments
}

function New-ConcatFile {
    param(
        [System.Collections.Generic.List[PSCustomObject]]$Segments,
        [string]$InputVideo,
        [string]$TempDir,
        [string]$FfmpegPath
    )
    
    $segmentFiles = [System.Collections.Generic.List[string]]::new()
    $index = 0
    
    foreach ($seg in $Segments) {
        $index++
        $segFile = Join-Path $TempDir "segment_$($index.ToString('D3')).mp4"
        
        $startStr = $seg.Start.ToString('0.###', [System.Globalization.CultureInfo]::InvariantCulture)
        $durStr = $seg.Duration.ToString('0.###', [System.Globalization.CultureInfo]::InvariantCulture)
        
        Write-Dbg "Extracting segment $index : $startStr -> $($seg.End.ToString('0.###')) (${durStr}s)"
        
        # Extract segment with re-encoding for clean cuts
        $args = @(
            "-hide_banner", "-loglevel", "warning",
            "-ss", $startStr,
            "-i", "`"$InputVideo`"",
            "-t", $durStr,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
            "-an",
            "-y", "`"$segFile`""
        )
        
        $result = Start-Process -FilePath $FfmpegPath -ArgumentList $args -NoNewWindow -Wait -PassThru
        
        if ($result.ExitCode -ne 0) {
            throw "Failed to extract segment $index"
        }
        
        $segmentFiles.Add($segFile)
    }
    
    # Create concat list file
    $concatFile = Join-Path $TempDir "concat_list.txt"
    $concatContent = $segmentFiles | ForEach-Object { "file '$_'" }
    $concatContent | Out-File -FilePath $concatFile -Encoding ASCII
    
    return @{
        ConcatFile   = $concatFile
        SegmentFiles = $segmentFiles
    }
}

function Merge-Segments {
    param(
        [string]$ConcatFile,
        [string]$OutputFile,
        [string]$FfmpegPath
    )
    
    $args = @(
        "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0",
        "-i", "`"$ConcatFile`"",
        "-c", "copy",
        "-y", "`"$OutputFile`""
    )
    
    $result = Start-Process -FilePath $FfmpegPath -ArgumentList $args -NoNewWindow -Wait -PassThru
    
    if ($result.ExitCode -ne 0) {
        throw "Failed to merge segments"
    }
}

function Convert-ToGif {
    param(
        [string]$InputVideo,
        [string]$OutputFile,
        [int]$Fps,
        [string]$Scale,
        [string]$FfmpegPath,
        [string]$TempDir
    )
    
    $palette = Join-Path $TempDir "palette.png"
    
    # Build scale filter
    $scaleFilter = if ($Scale) { "scale=$Scale`:flags=lanczos" } else { "scale=iw:ih:flags=lanczos" }
    
    Write-Info "Pass 1: Generating color palette..."
    
    # Pass 1: Generate optimized palette
    $args1 = @(
        "-hide_banner", "-loglevel", "warning",
        "-i", "`"$InputVideo`"",
        "-vf", "`"fps=$Fps,$scaleFilter,palettegen=stats_mode=diff:max_colors=256`"",
        "-update", "1",
        "-y", "`"$palette`""
    )
    
    $result1 = Start-Process -FilePath $FfmpegPath -ArgumentList $args1 -NoNewWindow -Wait -PassThru
    
    if ($result1.ExitCode -ne 0) {
        throw "Failed to generate palette"
    }
    
    Write-Info "Pass 2: Creating GIF with palette..."
    
    # Pass 2: Create GIF using palette
    $args2 = @(
        "-hide_banner", "-loglevel", "warning",
        "-i", "`"$InputVideo`"",
        "-i", "`"$palette`"",
        "-lavfi", "`"fps=$Fps,$scaleFilter [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle`"",
        "-y", "`"$OutputFile`""
    )
    
    $result2 = Start-Process -FilePath $FfmpegPath -ArgumentList $args2 -NoNewWindow -Wait -PassThru
    
    if ($result2.ExitCode -ne 0) {
        throw "Failed to create GIF"
    }
}

function Convert-ToWebp {
    param(
        [string]$InputVideo,
        [string]$OutputFile,
        [int]$Fps,
        [string]$Scale,
        [string]$FfmpegPath
    )
    
    $scaleFilter = if ($Scale) { "scale=$Scale" } else { "scale=iw:ih" }
    $filters = "fps=$Fps,$scaleFilter`:flags=lanczos"
    
    Write-Info "Generating animated WebP..."
    
    $args = @(
        "-hide_banner", "-loglevel", "warning",
        "-i", "`"$InputVideo`"",
        "-vf", "`"$filters`"",
        "-c:v", "libwebp", "-lossless", "0", "-q:v", "75", "-loop", "0",
        "-y", "`"$OutputFile`""
    )
    
    $result = Start-Process -FilePath $FfmpegPath -ArgumentList $args -NoNewWindow -Wait -PassThru
    
    if ($result.ExitCode -ne 0) {
        throw "Failed to create WebP"
    }
}

function Convert-ToMp4 {
    param(
        [string]$InputVideo,
        [string]$OutputFile,
        [int]$Fps,
        [string]$Scale,
        [string]$FfmpegPath
    )
    
    $scaleFilter = if ($Scale) { "scale=$Scale" } else { "scale=iw:ih" }
    $filters = "fps=$Fps,$scaleFilter`:flags=lanczos"
    
    Write-Info "Generating optimized MP4..."
    
    $args = @(
        "-hide_banner", "-loglevel", "warning",
        "-i", "`"$InputVideo`"",
        "-vf", "`"$filters`"",
        "-c:v", "libx264", "-preset", "slow", "-crf", "23", "-pix_fmt", "yuv420p",
        "-an",
        "-y", "`"$OutputFile`""
    )
    
    $result = Start-Process -FilePath $FfmpegPath -ArgumentList $args -NoNewWindow -Wait -PassThru
    
    if ($result.ExitCode -ne 0) {
        throw "Failed to create MP4"
    }
}

function Format-FileSize {
    param([long]$Bytes)
    
    if ($Bytes -ge 1MB) {
        return "{0:N2} MB" -f ($Bytes / 1MB)
    }
    elseif ($Bytes -ge 1KB) {
        return "{0:N2} KB" -f ($Bytes / 1KB)
    }
    else {
        return "$Bytes bytes"
    }
}

function Format-Duration {
    param([double]$Seconds)
    
    $ts = [TimeSpan]::FromSeconds($Seconds)
    if ($ts.TotalMinutes -ge 1) {
        return "{0}:{1:D2}.{2:D1}" -f [int]$ts.TotalMinutes, $ts.Seconds, [int]($ts.Milliseconds / 100)
    }
    else {
        return "{0:N1}s" -f $Seconds
    }
}

#endregion

#region ==================== MAIN SCRIPT ====================

try {
    $startTime = Get-Date
    
    Write-Host ""
    Write-Host "========================================" -ForegroundColor $Colors.Info
    Write-Host "  Screen Capture Optimizer v2.0" -ForegroundColor $Colors.Info
    Write-Host "========================================" -ForegroundColor $Colors.Info
    
    # ----- Find ffmpeg -----
    Write-Step "Finding ffmpeg..."
    $ff = Find-Ffmpeg -CustomPath $FfmpegPath
    Write-Ok "ffmpeg: $($ff.Ffmpeg)"
    Write-Ok "ffprobe: $($ff.Ffprobe)"
    
    # ----- Validate input -----
    Write-Step "Validating input..."
    
    if (-not (Test-Path -LiteralPath $InputFile)) {
        throw "Input file not found: $InputFile"
    }
    
    $InputFull = (Resolve-Path -LiteralPath $InputFile).Path
    $inputDir = Split-Path -Parent $InputFull
    $baseName = [System.IO.Path]::GetFileNameWithoutExtension($InputFull)
    
    Write-Ok "Input: $InputFull"
    
    # Get video info
    $duration = Get-VideoDuration -VideoPath $InputFull -FfprobePath $ff.Ffprobe
    $videoRes = Get-VideoResolution -VideoPath $InputFull -FfprobePath $ff.Ffprobe
    $inputSize = (Get-Item $InputFull).Length
    
    Write-Info "Duration: $(Format-Duration $duration)"
    Write-Info "Source: $($videoRes.Width)x$($videoRes.Height)"
    Write-Info "Size: $(Format-FileSize $inputSize)"
    
    # ----- Determine output path -----
    if (-not $OutputPath) {
        $ext = switch ($OutputFormat) {
            'Gif'  { 'gif' }
            'Webp' { 'webp' }
            'Mp4'  { 'mp4' }
        }
        $OutputPath = Join-Path $inputDir "${baseName}_optimized.$ext"
    }
    
    Write-Info "Output: $OutputPath"
    Write-Info "Format: $OutputFormat"
    
    # ----- Check if output file exists -----
    if (Test-Path -LiteralPath $OutputPath) {
        Write-Host ""
        Write-Warn "Output file already exists: $OutputPath"
        $response = Read-Host "    Overwrite? [y/N]"
        if ($response -notmatch '^[Yy]') {
            Write-Host ""
            Write-Host "    Aborted by user." -ForegroundColor $Colors.Warning
            exit 0
        }
    }
    
    # ----- Determine scale from Resolution -----
    $scaleParam = $null
    $resolutionWidth = 0
    
    # Resolution presets (width in pixels)
    $resPresets = @{
        '4K'      = 3840
        '2160p'   = 3840
        '2K'      = 2560
        '1440p'   = 2560
        'FHD'     = 1920
        '1080p'   = 1920
        'HD'      = 1280
        '720p'    = 1280
        '480p'    = 854
        '360p'    = 640
        '240p'    = 426
    }
    
    if ($Resolution -and $Resolution -ine 'Original') {
        # Check if it's a preset
        if ($resPresets.ContainsKey($Resolution.ToUpper()) -or $resPresets.ContainsKey($Resolution)) {
            $key = $resPresets.Keys | Where-Object { $_ -ieq $Resolution } | Select-Object -First 1
            $resolutionWidth = $resPresets[$key]
            Write-Info "Resolution: $Resolution ($($resolutionWidth)px width)"
        }
        # Check if it's a number
        elseif ($Resolution -match '^\d+$') {
            $resolutionWidth = [int]$Resolution
            Write-Info "Resolution: ${resolutionWidth}px width (custom)"
        }
        else {
            Write-Warn "Unknown resolution '$Resolution', keeping original"
            Write-Info "Valid presets: 4K, 2K, FHD, HD, 1080p, 720p, 480p, 360p, 240p, or a number"
        }
    }
    
    if ($resolutionWidth -gt 0) {
        $scaleParam = "${resolutionWidth}:-2"
        Write-Info "Scale: $scaleParam"
    }
    
    # ----- Create temp directory -----
    $tempDir = Join-Path $inputDir ".screencap_temp_$([System.IO.Path]::GetRandomFileName().Substring(0,8))"
    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
    Write-Dbg "Temp dir: $tempDir"
    
    # ----- Parse NoiseThreshold -----
    Write-Step "Configuring freeze detection..."
    
    $noiseMode = 'Default'
    [Nullable[double]]$noiseValue = $null
    
    if ($PSBoundParameters.ContainsKey('NoiseThreshold')) {
        if ($NoiseThreshold -ieq 'Auto') {
            $noiseMode = 'Auto'
            Write-Info "Mode: Auto (will try default, then 0.004 if no freezes)"
        }
        else {
            $parsed = 0.0
            if ([double]::TryParse($NoiseThreshold, [System.Globalization.NumberStyles]::Float, 
                [System.Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)) {
                
                # Clamp to safe range
                $parsed = [math]::Max(0.0001, [math]::Min(0.01, $parsed))
                $noiseValue = $parsed
                $noiseMode = 'Manual'
                Write-Info "Mode: Manual (n=$noiseValue)"
            }
            else {
                throw "Invalid NoiseThreshold: '$NoiseThreshold'. Use 'Auto' or a number (0.0001-0.01)."
            }
        }
    }
    else {
        Write-Info "Mode: Default (ffmpeg default threshold)"
    }
    
    Write-Info "Minimum freeze duration: ${FreezeMinSeconds}s"
    
    # ----- Detect freezes -----
    Write-Step "Detecting freeze frames..."
    
    $freezes = $null
    
    switch ($noiseMode) {
        'Default' {
            $output = Invoke-FreezeDetect -VideoPath $InputFull -MinDuration $FreezeMinSeconds -Noise $null -FfmpegPath $ff.Ffmpeg
            $freezes = ConvertTo-FreezeList -FfmpegOutput $output
        }
        'Manual' {
            $output = Invoke-FreezeDetect -VideoPath $InputFull -MinDuration $FreezeMinSeconds -Noise $noiseValue -FfmpegPath $ff.Ffmpeg
            $freezes = ConvertTo-FreezeList -FfmpegOutput $output
        }
        'Auto' {
            Write-Info "Auto pass 1: Using ffmpeg default threshold..."
            $output1 = Invoke-FreezeDetect -VideoPath $InputFull -MinDuration $FreezeMinSeconds -Noise $null -FfmpegPath $ff.Ffmpeg
            $freezes1 = ConvertTo-FreezeList -FfmpegOutput $output1
            
            if ($freezes1.Count -gt 0) {
                Write-Ok "Found $($freezes1.Count) freeze(s) with default threshold"
                $freezes = $freezes1
            }
            else {
                $autoNoise = 0.004
                Write-Info "Auto pass 2: Retrying with n=$autoNoise..."
                $output2 = Invoke-FreezeDetect -VideoPath $InputFull -MinDuration $FreezeMinSeconds -Noise $autoNoise -FfmpegPath $ff.Ffmpeg
                $freezes2 = ConvertTo-FreezeList -FfmpegOutput $output2
                
                if ($freezes2.Count -gt 0) {
                    Write-Ok "Found $($freezes2.Count) freeze(s) with n=$autoNoise"
                    $freezes = $freezes2
                    $noiseValue = $autoNoise
                }
                else {
                    Write-Warn "No freezes detected even with increased threshold"
                    $freezes = $freezes1
                }
            }
        }
    }
    
    # ----- Display freeze info -----
    if ($freezes.Count -eq 0) {
        Write-Ok "No freeze frames detected (>= ${FreezeMinSeconds}s)"
    }
    else {
        $totalFreezeTime = ($freezes | Measure-Object -Property Duration -Sum).Sum
        Write-Ok "Found $($freezes.Count) freeze segment(s), total: $(Format-Duration $totalFreezeTime)"
        
        if ($DebugFreezes) {
            Write-Host ""
            Write-Host "    Freeze Segments:" -ForegroundColor $Colors.Debug
            $freezes | ForEach-Object {
                Write-Host "      - $(Format-Duration $_.Start) -> $(Format-Duration $_.End) ($(Format-Duration $_.Duration))" -ForegroundColor $Colors.Debug
            }
        }
    }
    
    # ----- Process video -----
    Write-Step "Processing video..."
    
    $sourceForConversion = $InputFull
    
    if ($freezes.Count -gt 0) {
        # Calculate segments to keep
        $keepSegments = Get-KeepSegments -Freezes $freezes -TotalDuration $duration
        
        if ($DebugFreezes) {
            Write-Host ""
            Write-Host "    Keep Segments:" -ForegroundColor $Colors.Debug
            $keepSegments | ForEach-Object {
                Write-Host "      - $(Format-Duration $_.Start) -> $(Format-Duration $_.End) ($(Format-Duration $_.Duration))" -ForegroundColor $Colors.Debug
            }
        }
        
        if ($keepSegments.Count -eq 0) {
            throw "No content remaining after removing freezes!"
        }
        
        $keptDuration = ($keepSegments | Measure-Object -Property Duration -Sum).Sum
        Write-Info "Content duration: $(Format-Duration $keptDuration) (was $(Format-Duration $duration))"
        
        # Extract and concatenate segments
        Write-Info "Extracting $($keepSegments.Count) segment(s)..."
        $concatResult = New-ConcatFile -Segments $keepSegments -InputVideo $InputFull -TempDir $tempDir -FfmpegPath $ff.Ffmpeg
        
        Write-Info "Merging segments..."
        $mergedFile = Join-Path $tempDir "merged.mp4"
        Merge-Segments -ConcatFile $concatResult.ConcatFile -OutputFile $mergedFile -FfmpegPath $ff.Ffmpeg
        
        $sourceForConversion = $mergedFile
        Write-Ok "Freeze frames removed"
    }
    else {
        Write-Info "No freezes to remove, converting directly..."
    }
    
    # ----- Convert to output format -----
    Write-Step "Converting to $OutputFormat..."
    
    switch ($OutputFormat) {
        'Gif' {
            Convert-ToGif -InputVideo $sourceForConversion -OutputFile $OutputPath -Fps $Fps -Scale $scaleParam -FfmpegPath $ff.Ffmpeg -TempDir $tempDir
        }
        'Webp' {
            Convert-ToWebp -InputVideo $sourceForConversion -OutputFile $OutputPath -Fps $Fps -Scale $scaleParam -FfmpegPath $ff.Ffmpeg
        }
        'Mp4' {
            Convert-ToMp4 -InputVideo $sourceForConversion -OutputFile $OutputPath -Fps $Fps -Scale $scaleParam -FfmpegPath $ff.Ffmpeg
        }
    }
    
    # ----- Cleanup -----
    Write-Dbg "Cleaning up temp files..."
    Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue
    
    # ----- Summary -----
    $outputSize = (Get-Item $OutputPath).Length
    $elapsed = (Get-Date) - $startTime
    
    Write-Host ""
    Write-Host "========================================" -ForegroundColor $Colors.Success
    Write-Host "  COMPLETE!" -ForegroundColor $Colors.Success
    Write-Host "========================================" -ForegroundColor $Colors.Success
    Write-Host ""
    Write-Host "  Output: $OutputPath" -ForegroundColor $Colors.Success
    Write-Host "  Size: $(Format-FileSize $outputSize) (was $(Format-FileSize $inputSize))" -ForegroundColor $Colors.Info
    Write-Host "  Compression: $([math]::Round((1 - $outputSize / $inputSize) * 100, 1))% smaller" -ForegroundColor $Colors.Info
    Write-Host "  Time: $([math]::Round($elapsed.TotalSeconds, 1))s" -ForegroundColor $Colors.Info
    Write-Host ""
}
catch {
    Write-Host ""
    Write-Host "ERROR: $_" -ForegroundColor $Colors.Error
    Write-Host $_.ScriptStackTrace -ForegroundColor $Colors.Debug
    
    # Cleanup on error
    if ($tempDir -and (Test-Path $tempDir)) {
        Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    
    exit 1
}

#endregion
