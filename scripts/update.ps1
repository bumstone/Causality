param(
    [switch]$AllowDirty,
    [switch]$RefreshAgent,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$Arguments = @()
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

Push-Location $ProjectRoot
try {
    if (-not $AllowDirty) {
        $dirty = git status --porcelain
        if ($dirty) {
            throw "Working tree has local changes. Commit/stash them or rerun with -AllowDirty."
        }
    }

    $branch = (git branch --show-current).Trim()
    if (-not $branch) {
        throw "Detached HEAD is not supported by update.ps1."
    }

    Invoke-Checked "git" @("fetch", "origin")
    Invoke-Checked "git" @("pull", "--ff-only", "origin", $branch)

    if (-not (Test-Path $VenvPython)) {
        & (Join-Path $PSScriptRoot "install.ps1") -SkipDoctor
    }
    else {
        Invoke-Checked $VenvPython @("-m", "pip", "install", "-e", ".")
    }

    if ($RefreshAgent) {
        Invoke-Checked $VenvPython @("-m", "causality.cli", "install-agent", "--project", ".", "--force")
    }
    else {
        Invoke-Checked $VenvPython @("-m", "causality.cli", "install-agent", "--project", ".")
    }

    if (-not $SkipTests) {
        & (Join-Path $PSScriptRoot "doctor.ps1")
    }
}
finally {
    Pop-Location
}
