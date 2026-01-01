# Screen Capture Optimizer

This PowerShell script is intended to be used for optimization of a slow paced screen capture sessions, like demonstaring some UI elements in a product, expecially effective for text based terminals for example showing off Linux scripts sesions. 

## Purpose

Screen recordings often contain long periods of inactivity - waiting for pages to load, reading text, thinking time, or paused moments. This script:

1. **Detects** static/frozen frames using ffmpeg's `freezedetect` filter
2. **Removes** segments where nothing changes for longer than a threshold
3. **Converts** the result to an optimized output format (MP4 optimized is the smallest resulting file)

Perfect for:
- Creating demo GIFs for documentation
- Sharing screen recordings without boring pauses
- Reducing file sizes by cutting idle time
- Preparing tutorials where you want to skip loading screens

## Demo

### Before Optimization
*Original screen recording with pauses and idle time:*

[▶️ Watch Original Video (2:52, 11.96 MB)](demo/2025-12-09_23-57-27.mp4)

### After Optimization
*Same recording with frozen frames removed - notice how much shorter and more engaging it is:*

[▶️ Watch Optimized Video (0:54, 3.71 MB)](demo/2025-12-09_23-57-27_optimized.mp4)

> 💡 **Result:** 69% smaller file size, removed 2 minutes of idle time!

## Requirements

- **PowerShell 5.1+** (Windows) or PowerShell Core 7+ (cross-platform)
- **ffmpeg** and **ffprobe** binaries

### Getting ffmpeg

**Option 1: Automatic Download (Recommended)**

If ffmpeg is not found, the script will offer to download it automatically from GitHub.

**Option 2: Manual Download**

| Source | Description | Link |
|--------|-------------|------|
| **BtbN GitHub** | Automated daily builds, ZIP format, recommended | [github.com/BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases) |
| **gyan.dev** | Windows builds, 7z format, well-maintained | [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/) |

**Recommended downloads:**

- **From GitHub (BtbN)** - Latest stable:
  - `ffmpeg-n8.0-latest-win64-gpl-8.0.zip` (~200MB, full GPL build)

- **From gyan.dev**:
  - `ffmpeg-release-full.7z` (requires 7-zip to extract)

**Option 3: Package Managers**

```powershell
# Windows - Chocolatey
choco install ffmpeg-full

# Windows - Scoop
scoop install ffmpeg

# Windows - WinGet
winget install ffmpeg
```

The script automatically searches for ffmpeg in:
1. Custom path (if specified via `-FfmpegPath`)
2. Script directory
3. `%LOCALAPPDATA%\ffmpeg\bin`, `%ProgramFiles%\ffmpeg\bin`
4. System PATH

## How to Run

### Option 1: Run Directly from GitHub (No Download Required)

Run this one-liner in PowerShell to fetch and execute the script directly:

```powershell
# Fetch and run with your video file
irm https://raw.githubusercontent.com/MarvinFS/Public/main/screencapture-optimize/screencapture-optimize.ps1 | iex; screencapture-optimize -InputFile "your-recording.mp4"
```

Or with all options:

```powershell
irm https://raw.githubusercontent.com/MarvinFS/Public/main/screencapture-optimize/screencapture-optimize.ps1 | iex; screencapture-optimize -InputFile "video.mp4" -Resolution HD -OutputFormat Gif -NoiseThreshold Auto
```

### Option 2: Download and Run Locally

```powershell
# Download the script
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/MarvinFS/Public/main/screencapture-optimize/screencapture-optimize.ps1" -OutFile "screencapture-optimize.ps1"

# Run it
& .\screencapture-optimize.ps1 -InputFile "your-recording.mp4"
```

### Option 3: Run with Execution Policy Bypass

If you get execution policy errors:

```powershell
powershell -ExecutionPolicy Bypass -File ".\screencapture-optimize.ps1" -InputFile "your-recording.mp4"
```

Or from cmd.exe:

```cmd
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/MarvinFS/Public/main/screencapture-optimize/screencapture-optimize.ps1 | iex; screencapture-optimize -InputFile 'video.mp4'"
```

> **Note:** When the script path contains spaces, use `&` (call operator):
> ```powershell
> & "D:\My Scripts\screencapture-optimize.ps1" -InputFile "video.mp4"
> ```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `-InputFile` | String | (required) | Source video file path |
| `-FreezeMinSeconds` | Double | `7.0` | Minimum freeze duration to cut (seconds) |
| `-NoiseThreshold` | String | (none) | Freeze detection sensitivity (see below) |
| `-Fps` | Int | `5` | Output frames per second |
| `-Resolution` | String | `Original` | Output resolution: `4K`, `2K`, `FHD`, `HD`, `1080p`, `720p`, `480p`, `360p`, `240p`, or custom width |
| `-OutputFormat` | String | `Gif` | Output format: `Gif`, `Webp`, or `Mp4` |
| `-OutputPath` | String | (auto) | Custom output path |
| `-FfmpegPath` | String | (auto) | Path to ffmpeg binaries folder |
| `-DebugFreezes` | Switch | `$false` | Show detailed freeze detection info for reference|
---
> **Note:** Audio is automatically stripped from all outputs. The script produces silent video/animations only.

## NoiseThreshold - Detailed Explanation

The `NoiseThreshold` parameter controls how sensitive the freeze detection algorithm is when determining whether consecutive frames are "the same" (frozen) or "different" (moving).

### How It Works

ffmpeg's `freezedetect` filter compares frames and calculates a "difference score" between them. If this score is below the noise threshold (`n`), the frames are considered identical (frozen).

```
Lower threshold = Stricter = Only perfectly identical frames count as frozen
Higher threshold = Looser = Frames with minor differences still count as frozen
```

Acceptable to specify an exact threshold value. Acceptable range: **0.0001 to 0.01**

### Numeric Value Guide

| Value | Sensitivity | When to Use |
|-------|-------------|-------------|
| `0.0001` - `0.001`| Very Strict | Lossless recordings, PNG sequences, perfect source quality |
| `0.0005`          | Real world example normal screen recordings of a Linux terminal - only this small value works well along with 10s duration |
| `0.001` - `0.002` | Strict | High-quality H.264, OBS recordings at high bitrate |
| `0.002` - `0.003` | Moderate | Standard screen recordings, most use cases |
| `0.003` - `0.005` | Tolerant | HEVC/H.265 videos, compressed recordings, slight encoder noise |
| `0.005` - `0.008` | Very Tolerant | Heavily compressed video, webcam recordings with noise |
| `0.008` - `0.01`  | Maximum | Very noisy sources, last resort if nothing else works |

### Options

#### 1. Not Specified (Default)
```powershell
.\screencapture-optimize.ps1 -InputFile "video.mp4"
```
Uses ffmpeg's internal default (~0.002). Good for clean, high-quality recordings.

#### 2. "Auto" Mode
```powershell
.\screencapture-optimize.ps1 -InputFile "video.mp4" -NoiseThreshold Auto
```
**Recommended for most users.** The script will:
1. First try with ffmpeg default threshold
2. If no freezes detected, automatically retry with `n=0.004` (more tolerant)
3. Use whichever finds freezes

Best for: Unknown video quality, HEVC/H.265 videos, compressed recordings.

#### 3. Manual Numeric Value
```powershell
.\screencapture-optimize.ps1 -InputFile "video.mp4" -NoiseThreshold 0.003
```
Specify an exact threshold value. Acceptable range: **0.0001 to 0.01**
(Numeric Value Guide find above)

### Real-World Examples

#### Example 1: Clean OBS Recording (H.264, high bitrate)
```powershell
# Default works fine
.\screencapture-optimize.ps1 -InputFile "obs-recording.mp4"
```

#### Example 2: HEVC Screen Capture (Windows Game Bar, NVIDIA ShadowPlay)
```powershell
# HEVC often has subtle frame differences even on static content
.\screencapture-optimize.ps1 -InputFile "shadowplay.mp4" -NoiseThreshold Auto
# Or manually:
.\screencapture-optimize.ps1 -InputFile "shadowplay.mp4" -NoiseThreshold 0.004
```

#### Example 3: Freezes Not Detected (you see static frames but script doesn't find them)
```powershell
# Increase threshold gradually
.\screencapture-optimize.ps1 -InputFile "video.mp4" -NoiseThreshold 0.005
# Still not working? Try higher:
.\screencapture-optimize.ps1 -InputFile "video.mp4" -NoiseThreshold 0.008
```

#### Example 4: Too Many Freezes Detected (actual content being cut)
```powershell
# Decrease threshold to be stricter
.\screencapture-optimize.ps1 -InputFile "video.mp4" -NoiseThreshold 0.0005
```

#### Example 5: Debug to See What's Being Detected
```powershell
# Use -DebugFreezes to see exactly what's happening
.\screencapture-optimize.ps1 -InputFile "video.mp4" -NoiseThreshold Auto -DebugFreezes
```

Output will show:
```
    Freeze Segments:
      - 0.0s -> 5.1s (5.1s)
      - 11.4s -> 26.3s (14.9s)
      - 52.3s -> 1:04.5 (12.2s)
    
    Keep Segments:
      - 5.1s -> 11.4s (6.3s)
      - 26.3s -> 52.3s (26.0s)
      - 1:04.5 -> 2:30.0 (25.5s)
```

---

## Output Formats

### GIF (Default)
Best for: Documentation, GitHub READMEs, short clips
```powershell
.\screencapture-optimize.ps1 -InputFile "demo.mp4" -OutputFormat Gif -Resolution HD
```
- Uses 2-pass palette generation for optimal colors
- Bayer dithering for smooth gradients
- Typically larger than source for long videos
- No audio (GIF format doesn't support audio)

### WebP
Best for: Web use, better quality than GIF, smaller files
```powershell
.\screencapture-optimize.ps1 -InputFile "demo.mp4" -OutputFormat Webp -Resolution FHD
```
- Animated WebP with lossy compression
- Much smaller than GIF at similar quality
- Good browser support (except older Safari)
- No audio (animated WebP doesn't support audio)

### MP4
Best for: Sharing, maximum quality, smallest files
```powershell
.\screencapture-optimize.ps1 -InputFile "demo.mp4" -OutputFormat Mp4
```
- H.264 encoding with optimized settings
- Best compression ratio
- Audio is always stripped (all formats produce silent output)

---

## Usage Examples

> **Note:** Use `&` before the script path if it contains spaces.

### Basic Usage
```powershell
# Simplest form - detect freezes, output GIF
& .\screencapture-optimize.ps1 -InputFile "recording.mp4"
```

### Scale Down for Smaller File
```powershell
# Use resolution presets
.\screencapture-optimize.ps1 -InputFile "recording.mp4" -Resolution FHD
.\screencapture-optimize.ps1 -InputFile "recording.mp4" -Resolution HD
.\screencapture-optimize.ps1 -InputFile "recording.mp4" -Resolution 480p

# Or custom width in pixels
.\screencapture-optimize.ps1 -InputFile "recording.mp4" -Resolution 800
```

### Adjust Freeze Detection
```powershell
# Cut freezes longer than 2 seconds (default is 4)
.\screencapture-optimize.ps1 -InputFile "recording.mp4" -FreezeMinSeconds 2

# With auto noise threshold
.\screencapture-optimize.ps1 -InputFile "recording.mp4" -FreezeMinSeconds 2 -NoiseThreshold Auto
```

### Custom Output Location
```powershell
.\screencapture-optimize.ps1 -InputFile "recording.mp4" -OutputPath "C:\output\my-demo.gif"
```

### Higher FPS for Smoother Animation
```powershell
# Default is 10fps, increase to 15 for smoother playback
.\screencapture-optimize.ps1 -InputFile "recording.mp4" -Fps 15
```

### Full Example with All Options
```powershell
.\screencapture-optimize.ps1 `
    -InputFile "F:\recordings\2025-demo.mp4" `
    -OutputFormat Gif `
    -OutputPath "F:\output\demo.gif" `
    -Resolution HD `
    -Fps 12 `
    -FreezeMinSeconds 3 `
    -NoiseThreshold Auto `
    -DebugFreezes
```

### Batch Processing
```powershell
# Process all MP4 files in a folder
Get-ChildItem "F:\recordings\*.mp4" | ForEach-Object {
    .\screencapture-optimize.ps1 -InputFile $_.FullName -OutputFormat Gif -Resolution HD -NoiseThreshold Auto
}
```

---

## Output Example

```
========================================
  Screen Capture Optimizer v2.0
========================================

>>> Finding ffmpeg...
    [OK] ffmpeg: F:\ffmpeg\bin\ffmpeg.exe
    [OK] ffprobe: F:\ffmpeg\bin\ffprobe.exe

>>> Validating input...
    [OK] Input: F:\2025-12-09_23-57-27.mp4
    Duration: 2:52.5
    Resolution: 2560x1440
    Size: 11.96 MB
    Output: F:\2025-12-09_23-57-27_optimized.gif
    Format: Gif
    Resolution: HD (1280px width)
    Scale: 1280:-2

>>> Configuring freeze detection...
    Mode: Auto (will try default, then 0.004 if no freezes)
    Minimum freeze duration: 4s

>>> Detecting freeze frames...
    Auto pass 1: Using ffmpeg default threshold...
    [OK] Found 7 freeze(s) with default threshold
    [OK] Found 7 freeze segment(s), total: 58.8s

>>> Processing video...
    Content duration: 53.7s (was 2:52.5)
    Extracting 4 segment(s)...
    Merging segments...
    [OK] Freeze frames removed

>>> Converting to Gif...
    Pass 1: Generating color palette...
    Pass 2: Creating GIF with palette...

========================================
  COMPLETE!
========================================

  Output: F:\2025-12-09_23-57-27_optimized.gif
  Size: 3.71 MB (was 11.96 MB)
  Compression: 69% smaller
  Time: 15.8s
```

---

## Troubleshooting

### "No freezes detected"
- Try `-NoiseThreshold Auto` or a higher manual value (0.004, 0.006)
- Lower `-FreezeMinSeconds` to catch shorter pauses
- Use `-DebugFreezes` to see detection output

### "Too many freezes detected" (content being cut)
- Lower the threshold: `-NoiseThreshold 0.001`
- Increase `-FreezeMinSeconds` to only cut longer pauses

### GIF is larger than source
This is normal for long recordings. GIF format is inefficient for:
- Long videos (>30 seconds)
- High resolutions
- Complex color gradients

Solutions:
- Use `-Resolution 720p` or `480p` to reduce resolution
- Use `-Fps 4` to reduce frame count
- Use `-OutputFormat Webp` or `Mp4` instead

### Script fails with ffmpeg error
- Ensure ffmpeg version is recent (4.0+)
- Check the video file isn't corrupted
---

## License

MIT License - Free for personal and commercial use.
