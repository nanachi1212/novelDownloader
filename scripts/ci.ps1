[CmdletBinding()]
param(
    [ValidateSet('Auto', 'Full')]
    [string]$Mode = 'Auto',
    [string]$BaseRef = '',
    [string[]]$ChangedPath = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)] [string]$FilePath,
        [Parameter(Mandatory = $true)] [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

function Get-ChangedPaths {
    $paths = New-Object System.Collections.Generic.List[string]

    if ($ChangedPath -and $ChangedPath.Count -gt 0) {
        foreach ($path in $ChangedPath) {
            $paths.Add($path)
        }
    } elseif ($BaseRef) {
        $diff = & git diff --name-only --diff-filter=ACMRTUXB "$BaseRef...HEAD"
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to calculate changed paths against '$BaseRef'."
        }
        foreach ($path in $diff) {
            if ($path) { $paths.Add($path) }
        }
    } else {
        foreach ($path in (& git diff --name-only)) {
            if ($path) { $paths.Add($path) }
        }
        if ($LASTEXITCODE -ne 0) {
            throw 'Unable to calculate unstaged changed paths.'
        }
        foreach ($path in (& git diff --cached --name-only)) {
            if ($path) { $paths.Add($path) }
        }
        if ($LASTEXITCODE -ne 0) {
            throw 'Unable to calculate staged changed paths.'
        }
    }

    return @($paths | ForEach-Object { $_ -replace '\\', '/' } | Sort-Object -Unique)
}

try {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $PSCommandPath,
        [ref]$tokens,
        [ref]$parseErrors
    ) | Out-Null
    if ($parseErrors.Count -gt 0) {
        throw "PowerShell parser found $($parseErrors.Count) error(s) in $PSCommandPath."
    }

    $paths = @(Get-ChangedPaths)
    Write-Output "novelDownloader CI mode: $Mode"
    if ($paths.Count -eq 0) {
        Write-Warning 'WARN/NOT_TESTED: no changed paths were supplied.'
        exit 0
    }
    Write-Output ('Changed paths: ' + ($paths -join ', '))

    $pythonPaths = @($paths | Where-Object {
        $_ -match '^(tests|sites)/.*\.py$' -or
        $_ -match '^[^/]+\.py$' -or
        $_ -match '^requirements(-dev)?\.(txt|lock)$'
    })
    $packagingPaths = @($paths | Where-Object {
        $_ -match '^(packaging|tools)/' -or
        $_ -match '\.spec$'
    })

    if ($Mode -eq 'Full' -or $pythonPaths.Count -gt 0) {
        Invoke-Checked -FilePath 'python' -Arguments @('-m', 'pytest', '-q')
    } elseif ($packagingPaths.Count -gt 0) {
        Write-Warning 'WARN/NOT_TESTED: packaging changes need an explicit Windows build/smoke run.'
    } else {
        Write-Warning 'WARN/NOT_TESTED: changed paths do not require a novelDownloader test route.'
    }

    exit 0
} catch {
    Write-Error $_
    exit 1
}
