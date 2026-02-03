<#
.SYNOPSIS
    Pushes the public/ folder contents to the ClaudeBar public GitHub repo.

.DESCRIPTION
    This script takes the pre-prepared public/ folder (created by sync-public.ps1)
    and pushes it to https://github.com/MarvinFS/ClaudeBar

    By default opens a GUI for entering commit message. Use -NoGui for CLI mode.

.PARAMETER CommitMessage
    Custom commit message (CLI mode). If omitted, opens GUI or editor.

.PARAMETER NoGui
    Skip GUI and use editor for commit message instead.

.PARAMETER DryRun
    Show what would happen without making changes.

.PARAMETER CreateRelease
    Create a GitHub release after pushing. Requires -Version parameter.

.PARAMETER Version
    Version tag for the release (e.g., "v1.1.0")

.EXAMPLE
    .\publish-to-public.ps1  # Opens GUI

.EXAMPLE
    .\publish-to-public.ps1 -CommitMessage "Release v1.1.0" -CreateRelease -Version "v1.1.0"
#>

param(
    [Parameter(Mandatory = $false)]
    [string]$CommitMessage,

    [switch]$NoGui,

    [switch]$DryRun,

    [switch]$CreateRelease,

    [string]$Version
)

$ErrorActionPreference = "Stop"

$SourceFolder = Join-Path $PSScriptRoot "public"
$PublicRepoUrl = "https://github.com/MarvinFS/ClaudeBar.git"
$PublicRepoPath = Join-Path (Split-Path $PSScriptRoot -Parent) "ClaudeBar-public"
$ExePath = Join-Path $PSScriptRoot "dist\ClaudeBar.exe"

#region GUI Function

function Show-CommitMessageGui {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing

    # Create Form
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "Push to ClaudeBar Public Repo"
    $form.Size = New-Object System.Drawing.Size(500, 420)
    $form.StartPosition = "CenterScreen"
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.Font = New-Object System.Drawing.Font("Segoe UI", 9)

    # Title Label
    $titleLabel = New-Object System.Windows.Forms.Label
    $titleLabel.Location = New-Object System.Drawing.Point(15, 15)
    $titleLabel.Size = New-Object System.Drawing.Size(455, 25)
    $titleLabel.Text = "Push ClaudeBar public/ folder to GitHub"
    $titleLabel.Font = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)
    $form.Controls.Add($titleLabel)

    # Info Label
    $infoLabel = New-Object System.Windows.Forms.Label
    $infoLabel.Location = New-Object System.Drawing.Point(15, 45)
    $infoLabel.Size = New-Object System.Drawing.Size(455, 20)
    $infoLabel.Text = "Target: https://github.com/MarvinFS/ClaudeBar"
    $infoLabel.ForeColor = [System.Drawing.Color]::Gray
    $form.Controls.Add($infoLabel)

    # Commit Message Label
    $msgLabel = New-Object System.Windows.Forms.Label
    $msgLabel.Location = New-Object System.Drawing.Point(15, 75)
    $msgLabel.Size = New-Object System.Drawing.Size(455, 20)
    $msgLabel.Text = "Commit message (first line = subject, blank line, then body):"
    $form.Controls.Add($msgLabel)

    # Commit Message TextBox (multiline)
    $msgTextBox = New-Object System.Windows.Forms.TextBox
    $msgTextBox.Location = New-Object System.Drawing.Point(15, 97)
    $msgTextBox.Size = New-Object System.Drawing.Size(455, 150)
    $msgTextBox.Multiline = $true
    $msgTextBox.ScrollBars = "Vertical"
    $msgTextBox.AcceptsReturn = $true
    $msgTextBox.AcceptsTab = $false
    $msgTextBox.Font = New-Object System.Drawing.Font("Consolas", 10)
    $msgTextBox.Text = "Update from private repo`r`n`r`n- "
    $form.Controls.Add($msgTextBox)

    # Create Release Checkbox
    $releaseCheck = New-Object System.Windows.Forms.CheckBox
    $releaseCheck.Location = New-Object System.Drawing.Point(15, 260)
    $releaseCheck.Size = New-Object System.Drawing.Size(200, 25)
    $releaseCheck.Text = "Create GitHub Release"
    $form.Controls.Add($releaseCheck)

    # Version Label
    $versionLabel = New-Object System.Windows.Forms.Label
    $versionLabel.Location = New-Object System.Drawing.Point(220, 263)
    $versionLabel.Size = New-Object System.Drawing.Size(60, 20)
    $versionLabel.Text = "Version:"
    $form.Controls.Add($versionLabel)

    # Version TextBox
    $versionTextBox = New-Object System.Windows.Forms.TextBox
    $versionTextBox.Location = New-Object System.Drawing.Point(280, 260)
    $versionTextBox.Size = New-Object System.Drawing.Size(100, 25)
    $versionTextBox.Text = "v1.1.0"
    $form.Controls.Add($versionTextBox)

    # Dry Run Checkbox
    $dryRunCheck = New-Object System.Windows.Forms.CheckBox
    $dryRunCheck.Location = New-Object System.Drawing.Point(15, 295)
    $dryRunCheck.Size = New-Object System.Drawing.Size(200, 25)
    $dryRunCheck.Text = "Dry Run (preview only)"
    $form.Controls.Add($dryRunCheck)

    # Buttons
    $publishBtn = New-Object System.Windows.Forms.Button
    $publishBtn.Location = New-Object System.Drawing.Point(280, 335)
    $publishBtn.Size = New-Object System.Drawing.Size(90, 32)
    $publishBtn.Text = "Publish"
    $publishBtn.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $publishBtn.Font = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
    $form.AcceptButton = $publishBtn
    $form.Controls.Add($publishBtn)

    $cancelBtn = New-Object System.Windows.Forms.Button
    $cancelBtn.Location = New-Object System.Drawing.Point(380, 335)
    $cancelBtn.Size = New-Object System.Drawing.Size(90, 32)
    $cancelBtn.Text = "Cancel"
    $cancelBtn.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $form.CancelButton = $cancelBtn
    $form.Controls.Add($cancelBtn)

    # Focus on text box
    $form.Add_Shown({ $msgTextBox.Select() })

    # Show dialog
    $result = $form.ShowDialog()

    if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
        $msg = $msgTextBox.Text.Trim()
        if ([string]::IsNullOrWhiteSpace($msg)) {
            [System.Windows.Forms.MessageBox]::Show(
                "Please enter a commit message.",
                "Empty Message",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Warning
            )
            return $null
        }
        return @{
            CommitMessage = $msg
            DryRun = $dryRunCheck.Checked
            CreateRelease = $releaseCheck.Checked
            Version = $versionTextBox.Text.Trim()
        }
    }

    return $null
}

#endregion

#region Main

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  Publish ClaudeBar to Public Repo" -ForegroundColor Cyan
Write-Host "========================================`n" -ForegroundColor Cyan

# Check source folder exists
if (-not (Test-Path $SourceFolder)) {
    Write-Host "Error: public/ folder not found. Run sync-public.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "Source:      $SourceFolder" -ForegroundColor Gray
Write-Host "Public Repo: $PublicRepoPath" -ForegroundColor Gray
Write-Host "Remote:      $PublicRepoUrl" -ForegroundColor Gray

# Get commit message
if ([string]::IsNullOrEmpty($CommitMessage)) {
    if ($NoGui) {
        # CLI mode - open editor
        $tempFile = Join-Path $env:TEMP "COMMIT_EDITMSG_$(Get-Date -Format 'yyyyMMdd_HHmmss').txt"

        $template = @"

# Enter commit message above (first line = subject)
# Lines starting with # will be ignored
# Save and close file to continue, or delete all content to cancel
#
# Publishing ClaudeBar public/ folder to GitHub
"@
        Set-Content -Path $tempFile -Value $template -Encoding UTF8

        if (Get-Command "code" -ErrorAction SilentlyContinue) {
            Write-Host "Opening VS Code for commit message..." -ForegroundColor Cyan
            Start-Process -FilePath "code" -ArgumentList "--wait", $tempFile -Wait -NoNewWindow
        } else {
            Write-Host "Opening notepad for commit message..." -ForegroundColor Cyan
            Start-Process -FilePath "notepad" -ArgumentList $tempFile -Wait
        }

        $CommitMessage = (Get-Content -Path $tempFile -Raw -Encoding UTF8) -split "`n" |
            Where-Object { $_ -notmatch '^\s*#' } |
            ForEach-Object { $_.TrimEnd() } |
            Out-String
        $CommitMessage = $CommitMessage.Trim()

        Remove-Item $tempFile -Force -ErrorAction SilentlyContinue

        if ([string]::IsNullOrWhiteSpace($CommitMessage)) {
            Write-Host "Commit message empty - cancelled." -ForegroundColor Yellow
            exit 0
        }
    } else {
        # GUI mode
        $guiResult = Show-CommitMessageGui

        if ($null -eq $guiResult) {
            Write-Host "Operation cancelled." -ForegroundColor Yellow
            exit 0
        }

        $CommitMessage = $guiResult.CommitMessage
        if ($guiResult.DryRun) { $DryRun = $true }
        if ($guiResult.CreateRelease) {
            $CreateRelease = $true
            $Version = $guiResult.Version
        }
    }
}

if ($DryRun) {
    Write-Host "[DRY RUN MODE]" -ForegroundColor Magenta
}

Write-Host "`nCommit message:" -ForegroundColor White
Write-Host $CommitMessage -ForegroundColor Gray

# Get git identity from current repo
$gitUserName = git config --get user.name 2>$null
$gitUserEmail = git config --get user.email 2>$null

# Clone or update public repo
if (-not (Test-Path $PublicRepoPath)) {
    Write-Host "`n>>> Cloning public repo..." -ForegroundColor Yellow
    if ($DryRun) {
        Write-Host "[DRY RUN] Would clone: $PublicRepoUrl" -ForegroundColor Magenta
    } else {
        git clone $PublicRepoUrl $PublicRepoPath
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Failed to clone public repo"
            exit 1
        }
    }
} else {
    Write-Host "`n>>> Updating public repo..." -ForegroundColor Yellow
    if (-not $DryRun) {
        Push-Location $PublicRepoPath
        try {
            $lockFile = Join-Path $PublicRepoPath ".git\index.lock"
            if (Test-Path $lockFile) {
                Write-Host "    Removing stale git lock file..." -ForegroundColor DarkGray
                Remove-Item $lockFile -Force
            }

            $ErrorActionPreference = 'SilentlyContinue'
            git fetch origin *>&1 | Out-Null
            $branch = (git symbolic-ref --short HEAD 2>&1) -replace "`n|`r",""
            if (-not $branch -or $branch -match "fatal") { $branch = "main" }
            git reset --hard "origin/$branch" *>&1 | Out-Null
            $ErrorActionPreference = 'Continue'
        } finally {
            Pop-Location
        }
    }
}

# Configure git identity
if (-not $DryRun -and (Test-Path $PublicRepoPath)) {
    Push-Location $PublicRepoPath
    try {
        if ($gitUserName) { git config user.name $gitUserName 2>&1 | Out-Null }
        if ($gitUserEmail) { git config user.email $gitUserEmail 2>&1 | Out-Null }
    } finally {
        Pop-Location
    }
}

# Copy files from public/ to repo (excluding .git)
Write-Host "`n>>> Copying files..." -ForegroundColor Yellow
if ($DryRun) {
    Write-Host "[DRY RUN] Would copy public/ contents to $PublicRepoPath" -ForegroundColor Magenta
} else {
    # Remove all files except .git
    Get-ChildItem -Path $PublicRepoPath -Force |
        Where-Object { $_.Name -ne '.git' } |
        Remove-Item -Recurse -Force

    # Copy all files from public/
    Get-ChildItem -Path $SourceFolder -Force | ForEach-Object {
        if ($_.PSIsContainer) {
            Copy-Item $_.FullName $PublicRepoPath -Recurse -Force
        } else {
            Copy-Item $_.FullName $PublicRepoPath -Force
        }
    }

    $fileCount = (Get-ChildItem -Path $PublicRepoPath -Recurse -File | Where-Object { $_.DirectoryName -notmatch '\\\.git' }).Count
    Write-Host "    Copied $fileCount files" -ForegroundColor Green
}

# Wait for sync
Write-Host "`n>>> Waiting for file sync..." -ForegroundColor Yellow
Start-Sleep -Seconds 2

# Commit and push
Write-Host "`n>>> Committing changes..." -ForegroundColor Yellow

if ($DryRun) {
    Write-Host "[DRY RUN] Would commit with message above" -ForegroundColor Magenta
    Write-Host "[DRY RUN] Would push to origin" -ForegroundColor Magenta
} else {
    Push-Location $PublicRepoPath
    try {
        $oldErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"

        $null = git add -A 2>&1

        $status = git status --porcelain
        if ([string]::IsNullOrEmpty($status)) {
            $ErrorActionPreference = $oldErrorAction
            Write-Host "    No changes to commit" -ForegroundColor Yellow
        } else {
            $commitMsgFile = Join-Path $env:TEMP "git_commit_msg.txt"
            Set-Content -Path $commitMsgFile -Value $CommitMessage -Encoding UTF8 -NoNewline

            $commitOutput = git commit -F $commitMsgFile 2>&1
            $commitExitCode = $LASTEXITCODE
            Remove-Item $commitMsgFile -Force -ErrorAction SilentlyContinue

            $ErrorActionPreference = $oldErrorAction

            if ($commitExitCode -ne 0) {
                Write-Host $commitOutput -ForegroundColor Red
                Write-Error "Failed to commit"
                exit 1
            }
            Write-Host $commitOutput

            Write-Host "`n>>> Pushing to remote..." -ForegroundColor Yellow

            $ErrorActionPreference = "Continue"
            $pushOutput = git push origin HEAD 2>&1
            $pushExitCode = $LASTEXITCODE
            $ErrorActionPreference = $oldErrorAction

            if ($pushExitCode -ne 0) {
                Write-Host $pushOutput -ForegroundColor Red
                Write-Error "Failed to push"
                exit 1
            }
            $pushOutput | Where-Object { $_ -notmatch '(remote:|Writing objects|Enumerating|Counting|Compressing|Total)' } | ForEach-Object {
                Write-Host $_
            }
        }
    } finally {
        Pop-Location
    }
}

# Create release if requested
if ($CreateRelease -and -not [string]::IsNullOrEmpty($Version)) {
    Write-Host "`n>>> Creating GitHub release $Version..." -ForegroundColor Yellow

    if ($DryRun) {
        Write-Host "[DRY RUN] Would create release $Version with ClaudeBar.exe" -ForegroundColor Magenta
    } else {
        Push-Location $PublicRepoPath
        try {
            if (Test-Path $ExePath) {
                $releaseNotes = @"
## ClaudeBar $Version

Windows system tray app for tracking Claude Code and Codex CLI usage.

### Downloads
- **ClaudeBar.exe** - Signed Windows executable
"@
                gh release create $Version $ExePath --title "ClaudeBar $Version" --notes $releaseNotes
                Write-Host "    Release created: $Version" -ForegroundColor Green
            } else {
                Write-Host "    Warning: ClaudeBar.exe not found at $ExePath" -ForegroundColor Yellow
                Write-Host "    Creating release without binary..." -ForegroundColor Yellow
                gh release create $Version --title "ClaudeBar $Version" --notes "ClaudeBar $Version release"
            }
        } finally {
            Pop-Location
        }
    }
}

Write-Host "`n========================================" -ForegroundColor Green
Write-Host "  COMPLETE!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host "`nPublic repo: $PublicRepoUrl" -ForegroundColor Gray

#endregion
