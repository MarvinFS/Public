# Sign ClaudeBar executable with MarvinFS certificate
$ErrorActionPreference = "Stop"

$exePath = "D:\OneDrive - NoWay Inc\APPS\scripts\claudebar\dist\ClaudeBar.exe"

# Find the MarvinFS certificate
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object { $_.Subject -like '*MarvinFS*' }

if (-not $cert) {
    Write-Error "MarvinFS code signing certificate not found!"
    exit 1
}

Write-Host "Using certificate: $($cert.Subject)"
Write-Host "Thumbprint: $($cert.Thumbprint)"
Write-Host "Signing: $exePath"

$result = Set-AuthenticodeSignature -FilePath $exePath -Certificate $cert -TimestampServer "http://timestamp.digicert.com"

if ($result.Status -eq "Valid") {
    Write-Host "Signature successful!" -ForegroundColor Green
    Write-Host "Status: $($result.Status)"
} else {
    Write-Host "Signature result: $($result.Status)" -ForegroundColor Yellow
    Write-Host "Message: $($result.StatusMessage)"
}
