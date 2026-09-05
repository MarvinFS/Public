# Sign the ClaudeBar executable with the MarvinFS code-signing certificate.
#
# Uses signtool.exe (NCrypt path) rather than Set-AuthenticodeSignature: the
# MarvinFS key lives in the legacy "Microsoft Strong Cryptographic Provider"
# CSP, which cannot perform SHA256 signing via CryptoAPI, so
# Set-AuthenticodeSignature (defaults to SHA256) fails with "The hash
# algorithm is not supported." signtool negotiates SHA256 against the same key.
param(
    [string]$ExePath = (Join-Path $PSScriptRoot "dist\ClaudeBar.exe"),
    [string]$TimestampServer = "http://timestamp.digicert.com"
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path $ExePath)) {
    Write-Error "Executable not found: $ExePath (run build.ps1 first)"
    exit 1
}

# Locate the MarvinFS code-signing certificate by subject.
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
    Where-Object { $_.Subject -like '*MarvinFS*' } |
    Select-Object -First 1
if (-not $cert) {
    Write-Error "MarvinFS code signing certificate not found in Cert:\CurrentUser\My"
    exit 1
}

# Locate the newest x64 signtool.exe under the Windows 10/11 SDK.
$signtool = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin" -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\x64\\' } |
    Sort-Object FullName -Descending |
    Select-Object -First 1
if (-not $signtool) {
    Write-Error "signtool.exe not found. Install the Windows 10/11 SDK."
    exit 1
}

Write-Host "Certificate : $($cert.Subject)"
Write-Host "Thumbprint  : $($cert.Thumbprint)"
Write-Host "signtool    : $($signtool.FullName)"
Write-Host "Signing     : $ExePath"

& $signtool.FullName sign /sha1 $cert.Thumbprint /fd SHA256 /td SHA256 /tr $TimestampServer /v $ExePath
if ($LASTEXITCODE -ne 0) {
    Write-Error "signtool failed with exit code $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Host "Signature successful (SHA256 + timestamp)." -ForegroundColor Green
