[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$PackagePath,
    [Parameter(Mandatory)] [string]$CertificatePath,
    [switch]$InstallMachineTrust,
    [switch]$Launch
)
$ErrorActionPreference = 'Stop'
$package = (Resolve-Path -LiteralPath $PackagePath).Path
$certificate = (Resolve-Path -LiteralPath $CertificatePath).Path
Import-Certificate -FilePath $certificate -CertStoreLocation 'Cert:\CurrentUser\TrustedPeople' | Out-Null
if ($InstallMachineTrust) {
    Import-Certificate -FilePath $certificate -CertStoreLocation 'Cert:\LocalMachine\TrustedPeople' | Out-Null
}
try {
    Add-AppxPackage -Path $package -ForceApplicationShutdown -ErrorAction Stop
} catch {
    throw "MSIX installation failed: $($_.Exception.Message)"
}
$installed = Get-AppxPackage -Name 'novelDownloader.Dev' | Select-Object -First 1
if (-not $installed) { throw 'MSIX installation reported success but the package is not registered.' }
if ($Launch) {
    Start-Process explorer.exe "shell:AppsFolder\$($installed.PackageFamilyName)!App"
}
Write-Output 'Development MSIX installed.'
