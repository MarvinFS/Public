# ClaudeBar Build Script
# Builds standalone .exe using PyInstaller

param(
    [switch]$Clean,
    [switch]$Install
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$SrcDir = Join-Path $ProjectRoot "src"
$BuildDir = Join-Path $ProjectRoot "build"
$DistDir = Join-Path $ProjectRoot "dist"
$VenvDir = Join-Path $ProjectRoot ".venv"

Write-Host "ClaudeBar Build Script" -ForegroundColor Cyan
Write-Host "======================" -ForegroundColor Cyan

# Clean previous builds
if ($Clean) {
    Write-Host "`nCleaning previous builds..." -ForegroundColor Yellow
    if (Test-Path $DistDir) { Remove-Item -Recurse -Force $DistDir }
    if (Test-Path (Join-Path $BuildDir "ClaudeBar")) { Remove-Item -Recurse -Force (Join-Path $BuildDir "ClaudeBar") }
    Write-Host "Clean complete." -ForegroundColor Green
}

# Create virtual environment if needed
if (-not (Test-Path $VenvDir)) {
    Write-Host "`nCreating virtual environment..." -ForegroundColor Yellow
    python -m venv $VenvDir
}

# Activate venv and install dependencies
Write-Host "`nInstalling dependencies..." -ForegroundColor Yellow
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip = Join-Path $VenvDir "Scripts\pip.exe"

& $VenvPip install --upgrade pip
& $VenvPip install -r (Join-Path $ProjectRoot "requirements.txt")

# Build with PyInstaller
Write-Host "`nBuilding executable..." -ForegroundColor Yellow
$SpecFile = Join-Path $BuildDir "claudebar.spec"

Push-Location $ProjectRoot
try {
    & $VenvPython -m PyInstaller --clean --noconfirm $SpecFile
} finally {
    Pop-Location
}

# Check result
$ExePath = Join-Path $DistDir "ClaudeBar.exe"
if (Test-Path $ExePath) {
    $Size = (Get-Item $ExePath).Length / 1MB
    Write-Host "`nBuild successful!" -ForegroundColor Green
    Write-Host "Output: $ExePath" -ForegroundColor Green
    Write-Host "Size: $([math]::Round($Size, 2)) MB" -ForegroundColor Green

    # Optional install to user's startup
    if ($Install) {
        $StartupDir = [Environment]::GetFolderPath("Startup")
        $DestPath = Join-Path $StartupDir "ClaudeBar.exe"
        Copy-Item $ExePath $DestPath -Force
        Write-Host "`nInstalled to startup: $DestPath" -ForegroundColor Green
    }
} else {
    Write-Host "`nBuild failed - executable not found" -ForegroundColor Red
    exit 1
}
