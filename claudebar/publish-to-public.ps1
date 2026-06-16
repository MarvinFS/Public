<#
.SYNOPSIS
    Publishes ClaudeBar to the public GitHub repo (MarvinFS/Public).

.DESCRIPTION
    Thin wrapper around the workspace-level publish-to-public.ps1 script.
    Delegates to the parent script with SourceFolder="claudebar".
    Optionally creates a GitHub release with the signed ClaudeBar.exe.

.PARAMETER CommitMessage
    Custom commit message. If omitted, opens GUI via parent script.

.PARAMETER DryRun
    Show what would happen without making changes.

.PARAMETER CreateRelease
    Create a GitHub release after pushing. Requires -Version parameter.

.PARAMETER Version
    Version tag for the release (e.g., "claudebar-v1.2.0")

.EXAMPLE
    .\publish-to-public.ps1 -CommitMessage "feat: update v1.2.0"

.EXAMPLE
    .\publish-to-public.ps1 -CommitMessage "feat: update" -CreateRelease -Version "claudebar-v1.2.0"

.EXAMPLE
    .\publish-to-public.ps1  # Opens GUI via parent script
#>

param(
    [Parameter(Mandatory = $false)]
    [string]$CommitMessage,

    [switch]$DryRun,

    [switch]$CreateRelease,

    [string]$Version
)

$ErrorActionPreference = "Stop"

$WorkspaceScript = Join-Path (Split-Path $PSScriptRoot -Parent) "publish-to-public.ps1"
$ExePath = Join-Path $PSScriptRoot "dist\ClaudeBar.exe"

if (-not (Test-Path $WorkspaceScript)) {
    Write-Error "Workspace publish script not found: $WorkspaceScript"
    exit 1
}

# Build arguments for the workspace script
$scriptArgs = @{
    SourceFolder = "claudebar"
}

if (-not [string]::IsNullOrEmpty($CommitMessage)) {
    $scriptArgs["CommitMessage"] = $CommitMessage
} else {
    $scriptArgs["Gui"] = $true
}

if ($DryRun) {
    $scriptArgs["DryRun"] = $true
}

Write-Host "Delegating to workspace publish script..." -ForegroundColor Cyan
& $WorkspaceScript @scriptArgs

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

# Create release if requested
if ($CreateRelease -and -not [string]::IsNullOrEmpty($Version)) {
    Write-Host "`n>>> Creating GitHub release $Version..." -ForegroundColor Yellow

    if ($DryRun) {
        Write-Host "[DRY RUN] Would create release $Version" -ForegroundColor Magenta
    } else {
        if (Test-Path $ExePath) {
            $releaseTitle = $Version -replace 'claudebar-',''
            gh release create $Version $ExePath -R "MarvinFS/Public" --title "ClaudeBar $releaseTitle" --notes "ClaudeBar $releaseTitle release with signed Windows executable."
            Write-Host "    Release created: $Version" -ForegroundColor Green
        } else {
            Write-Host "    Warning: ClaudeBar.exe not found at $ExePath" -ForegroundColor Yellow
            Write-Host "    Run build.ps1 first." -ForegroundColor Yellow
        }
    }
}
