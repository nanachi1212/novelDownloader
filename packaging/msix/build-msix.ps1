[CmdletBinding()]
param(
    [ValidateSet('Development', 'Validation', 'Store')]
    [string]$Mode = 'Development',
    [string]$IdentityName,
    [string]$Publisher,
    [string]$PublisherDisplayName = 'novelDownloader',
    [string]$PackageVersion = '1.6.8.0',
    [ValidateSet('x64', 'x86', 'arm64')]
    [string]$Architecture = 'x64',
    [switch]$KeepDevelopmentCertificate
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$work = Join-Path $PSScriptRoot '.work'
$stage = Join-Path $work 'package'
$assets = Join-Path $stage 'Assets'
$output = Join-Path $root 'dist\msix'
$spec = Join-Path $root 'novelDownloader.spec'
$python = Join-Path $root '.venv\Scripts\python.exe'
$pythonFromPath = Get-Command 'python.exe' -ErrorAction SilentlyContinue
$manifestTemplate = Join-Path $PSScriptRoot 'AppxManifest.xml.template'
$manifest = Join-Path $stage 'AppxManifest.xml'

function Invoke-Native([string]$FilePath, [string[]]$ArgumentList) {
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) { throw "$FilePath failed with exit code $LASTEXITCODE" }
}

function Find-SdkTool([string]$Name) {
    $kitCandidates = foreach ($kitRoot in @(
        'C:\Program Files (x86)\Windows Kits\10\bin',
        'C:\Program Files\Windows Kits\10\bin'
    )) {
        if (Test-Path -LiteralPath $kitRoot) {
            Get-ChildItem -LiteralPath $kitRoot -Directory -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object { Join-Path $_.FullName "x64\$Name" }
        }
    }
    $candidates = @(
        (Get-Command $Name -ErrorAction SilentlyContinue).Source,
        $kitCandidates,
        "C:\Program Files (x86)\Windows Kits\10\App Certification Kit\$Name"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    if (-not $candidates) { throw "$Name was not found. Install the Windows 10/11 SDK." }
    return $candidates[0]
}

if (-not (Test-Path -LiteralPath $python)) {
    if (-not $pythonFromPath -or -not (Test-Path -LiteralPath $pythonFromPath.Source)) {
        throw "Missing project Python and no python.exe was found on PATH: $python"
    }
    $python = $pythonFromPath.Source
}
if ($Architecture -ne 'x64') {
    throw "Architecture '$Architecture' is not supported by the current x64 Python/PyInstaller environment. Use x64 or provide an architecture-matched build environment."
}
if ($PackageVersion -notmatch '^\d+\.\d+\.\d+\.\d+$') { throw 'PackageVersion must have four numeric components.' }
if ($Mode -eq 'Store' -and (-not $IdentityName -or -not $Publisher)) {
    throw 'Store mode requires the Partner Center Identity Name and Publisher. No development identity is used for Store mode.'
}
if (-not $IdentityName) { $IdentityName = if ($Mode -eq 'Development') { 'novelDownloader.Dev' } else { 'novelDownloader.Validation' } }
if (-not $Publisher) { $Publisher = if ($Mode -eq 'Development') { 'CN=novelDownloader Development' } else { 'CN=novelDownloader Validation' } }

if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
if (Test-Path -LiteralPath $output) { Remove-Item -LiteralPath $output -Recurse -Force }
New-Item -ItemType Directory -Path $stage, $assets, $output -Force | Out-Null

Push-Location $root
try {
    Invoke-Native $python @('-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', (Join-Path $work 'dist'), '--workpath', (Join-Path $work 'build'), $spec)
    $built = Join-Path $work 'dist\novelDownloader'
    if (-not (Test-Path -LiteralPath (Join-Path $built 'novelDownloader.exe'))) { throw 'PyInstaller did not produce novelDownloader.exe.' }
    Get-ChildItem -LiteralPath $built -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $stage -Recurse -Force
    }
    Invoke-Native $python @( (Join-Path $PSScriptRoot 'generate_assets.py'), $assets )
} finally { Pop-Location }

function XmlEscape([string]$Value) { return [System.Security.SecurityElement]::Escape($Value) }
$xml = Get-Content -LiteralPath $manifestTemplate -Raw
$replacements = @{
    '__IDENTITY_NAME__' = (XmlEscape $IdentityName)
    '__PUBLISHER__' = (XmlEscape $Publisher)
    '__PUBLISHER_DISPLAY_NAME__' = (XmlEscape $PublisherDisplayName)
    '__VERSION__' = $PackageVersion
    '__ARCHITECTURE__' = $Architecture
}
foreach ($key in $replacements.Keys) { $xml = $xml.Replace($key, $replacements[$key]) }
[IO.File]::WriteAllText($manifest, $xml, [Text.UTF8Encoding]::new($false))

$makeappx = Find-SdkTool 'makeappx.exe'
$package = Join-Path $output "novelDownloader_$($PackageVersion)_$($Architecture).msix"
Invoke-Native $makeappx @('pack', '/d', $stage, '/p', $package, '/o')

if ($Mode -eq 'Development') {
    $signtool = Find-SdkTool 'signtool.exe'
    $cer = Join-Path $output 'novelDownloader-development.cer'
    $pfx = Join-Path $work 'development.pfx'
    $passwordText = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    $password = ConvertTo-SecureString $passwordText -AsPlainText -Force
    $cert = New-SelfSignedCertificate -Type Custom -Subject $Publisher -CertStoreLocation 'Cert:\CurrentUser\My' -KeyUsage DigitalSignature -FriendlyName 'novelDownloader development MSIX'
    try {
        Export-Certificate -Cert $cert -FilePath $cer -Force | Out-Null
        Export-PfxCertificate -Cert $cert -FilePath $pfx -Password $password -Force | Out-Null
        Invoke-Native $signtool @('sign', '/fd', 'SHA256', '/f', $pfx, '/p', $passwordText, $package)
    } finally {
        Remove-Item -LiteralPath $pfx -Force -ErrorAction SilentlyContinue
        if (-not $KeepDevelopmentCertificate) {
            Remove-Item -LiteralPath ("Cert:\CurrentUser\My\$($cert.Thumbprint)") -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Output "MSIX: $package"
Write-Output "Mode: $Mode"
Write-Output "Identity: $IdentityName"
Write-Output "Publisher: $Publisher"
