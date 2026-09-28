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
$manifestIdentity = $null
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($package)
try {
    $manifestEntry = $archive.Entries | Where-Object { $_.FullName -eq 'AppxManifest.xml' } | Select-Object -First 1
    if (-not $manifestEntry) { throw 'AppxManifest.xml was not found in the supplied MSIX.' }
    $reader = [IO.StreamReader]::new($manifestEntry.Open())
    try {
        $manifestXml = [xml]$reader.ReadToEnd()
    } finally {
        $reader.Dispose()
    }
    $manifestIdentity = $manifestXml.SelectSingleNode("/*[local-name()='Package']/*[local-name()='Identity']").Name
} finally {
    $archive.Dispose()
}
if (-not $manifestIdentity) { throw 'The supplied MSIX manifest has no package identity.' }
Import-Certificate -FilePath $certificate -CertStoreLocation 'Cert:\CurrentUser\TrustedPeople' | Out-Null
if ($InstallMachineTrust) {
    Import-Certificate -FilePath $certificate -CertStoreLocation 'Cert:\LocalMachine\TrustedPeople' | Out-Null
}
try {
    Add-AppxPackage -Path $package -ForceApplicationShutdown -ErrorAction Stop
} catch {
    throw "MSIX installation failed: $($_.Exception.Message)"
}
$installed = Get-AppxPackage -Name $manifestIdentity | Select-Object -First 1
if (-not $installed) { throw 'MSIX installation reported success but the package is not registered.' }
if ($Launch) {
    Start-Process explorer.exe "shell:AppsFolder\$($installed.PackageFamilyName)!App"
}
Write-Output 'Development MSIX installed.'
