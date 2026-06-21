param(
    [switch]$NpmOnly
)

$ErrorActionPreference = "Stop"

$codex = Get-Command codex -ErrorAction SilentlyContinue
$npm = Get-Command npm -ErrorAction SilentlyContinue

if ($npm) {
    $globalPackages = npm list -g --depth=0 2>$null
    if ($globalPackages -match "@openai/codex") {
        npm install -g @openai/codex@latest
        exit $LASTEXITCODE
    }
}

if ($NpmOnly) {
    if (-not $npm) {
        throw "npm was not found on PATH."
    }
    npm install -g @openai/codex@latest
    exit $LASTEXITCODE
}

if (-not $codex) {
    Write-Host "codex was not found on PATH. Running the official installer."
}
else {
    Write-Host "codex is installed, but not through npm. Running the official installer/update path."
}

powershell -ExecutionPolicy ByPass -Command "irm https://chatgpt.com/codex/install.ps1 | iex"
