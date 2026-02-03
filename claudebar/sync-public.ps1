# sync-public.ps1 - Sync source files to public/ for GitHub release
# Usage: .\sync-public.ps1
#
# This script copies public-release files to public/ folder, excluding:
# - Private documentation (CLAUDE.md, session docs)
# - Development tools (.claude-scripts/, code-review files)
# - Build artifacts (dist/, build/, __pycache__/)

$ErrorActionPreference = "Stop"

$source = $PSScriptRoot
$dest = Join-Path $source "public"

Write-Host "Syncing to: $dest" -ForegroundColor Cyan

# Clear destination completely (remove and recreate to avoid stale files)
if (Test-Path $dest) {
    Write-Host "Removing existing public/ folder..."
    Remove-Item $dest -Recurse -Force
}
Write-Host "Creating fresh public/ folder..."
New-Item -ItemType Directory -Path $dest | Out-Null

# Files/folders to copy
$items = @(
    "src",
    "resources",
    "tests",
    "README.md",
    "requirements.txt",
    "ClaudeBar.spec",
    "pytest.ini",
    "sign-exe.ps1"
)

foreach ($item in $items) {
    $srcPath = Join-Path $source $item
    if (Test-Path $srcPath) {
        $destPath = Join-Path $dest $item
        if ((Get-Item $srcPath).PSIsContainer) {
            Copy-Item $srcPath $dest -Recurse -Force
        } else {
            Copy-Item $srcPath $destPath -Force
        }
        Write-Host "  Copied: $item" -ForegroundColor Green
    } else {
        Write-Host "  Skipped (not found): $item" -ForegroundColor Yellow
    }
}

# Clean __pycache__, build artifacts from copied content
Write-Host "`nCleaning build artifacts..."
$cleanPatterns = @("__pycache__", "build", "*.egg-info", ".git", ".github", ".pytest_cache")
foreach ($pattern in $cleanPatterns) {
    Get-ChildItem $dest -Recurse -Directory -Filter $pattern -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "  Removed: $($_.FullName.Replace($dest, ''))" -ForegroundColor DarkGray
        Remove-Item $_.FullName -Recurse -Force
    }
}

# Remove .pyc files
Get-ChildItem $dest -Recurse -Filter "*.pyc" -ErrorAction SilentlyContinue | ForEach-Object {
    Remove-Item $_.FullName -Force
}

# Create a minimal .gitignore for the public folder
$publicGitignore = @"
# Python
__pycache__/
*.py[cod]
*.so
build/
dist/
*.egg-info/

# Virtual environments
.venv/
venv/

# IDE
.idea/
.vscode/

# OS
.DS_Store
Thumbs.db

# Logs
*.log

# Local config
.env
"@

Set-Content -Path (Join-Path $dest ".gitignore") -Value $publicGitignore

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "Sync complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "`nFiles in public/:"
Get-ChildItem $dest | ForEach-Object {
    if ($_.PSIsContainer) {
        $count = (Get-ChildItem $_.FullName -Recurse -File).Count
        Write-Host "  [DIR]  $($_.Name) ($count files)"
    } else {
        Write-Host "  [FILE] $($_.Name) ($([math]::Round($_.Length/1KB, 1)) KB)"
    }
}

# Calculate total size
$totalSize = (Get-ChildItem $dest -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host "`nTotal size: $([math]::Round($totalSize/1MB, 2)) MB" -ForegroundColor Cyan
