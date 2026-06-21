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
    if (-not (Test-Path $VenvPython)) {
        throw "Virtual environment is missing. Run scripts\install.ps1 first."
    }

    Write-Host "== Git =="
    Invoke-Checked "git" @("status", "--short", "--branch")

    Write-Host "`n== Python =="
    Invoke-Checked $VenvPython @("--version")
    Invoke-Checked $VenvPython @("-m", "pip", "--version")

    Write-Host "`n== Causality CLI =="
    Invoke-Checked $VenvPython @("-m", "causality.cli", "manifest", "--pretty")
    Invoke-Checked $VenvPython @("-m", "causality.cli", "context", "--pretty")

    Write-Host "`n== Tests =="
    Invoke-Checked $VenvPython @("-m", "unittest", "discover", "-s", "tests")

    Write-Host "`n== Optional tools =="
    $node = Get-Command node -ErrorAction SilentlyContinue
    if ($node) {
        node --version
    }
    else {
        Write-Host "node: not found"
    }

    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if ($npm) {
        npm --version
    }
    else {
        Write-Host "npm: not found"
    }

    $codex = Get-Command codex -ErrorAction SilentlyContinue
    if ($codex) {
        codex --version
    }
    else {
        Write-Host "codex: not found"
    }
}
finally {
    Pop-Location
}
