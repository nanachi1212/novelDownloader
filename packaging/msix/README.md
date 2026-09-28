# MSIX packaging

`build-msix.ps1` builds the existing PyInstaller onedir application, stages it as a packaged classic Win32 app, generates local PNG assets, and calls the Windows SDK `MakeAppx.exe`.

## Local development package

From any working directory:

```powershell
pwsh .\packaging\msix\build-msix.ps1 -Mode Development
pwsh .\packaging\msix\install-dev-msix.ps1 `
  -PackagePath .\dist\msix\novelDownloader_1.6.8.0_x64.msix `
  -CertificatePath .\dist\msix\novelDownloader-development.cer -Launch
```

The development certificate is for local testing only. The script creates a temporary PFX with a random password, signs the package, and removes the PFX. On Windows configurations where AppX deployment requires system-level trust, run PowerShell as Administrator and add `-InstallMachineTrust`; this only imports the public `.cer` into `LocalMachine\TrustedPeople`. Do not submit the development identity to the Store.

## Store package

After Partner Center supplies the real identity values, build an unsigned Store-ready MSIX with no private certificate:

```powershell
pwsh .\packaging\msix\build-msix.ps1 -Mode Store `
  -IdentityName '<Partner Center Identity Name>' `
  -Publisher '<Partner Center Publisher>' `
  -PublisherDisplayName '<Publisher display name>' `
  -PackageVersion '1.6.8.0'
```

The Store submission is MSIX. Microsoft Store re-signs the accepted package; no paid code-signing certificate is required.
