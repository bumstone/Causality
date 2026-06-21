param(
    [switch]$RecreateVenv,
    [switch]$SkipAgent,
    [switch]$SkipDoctor
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPath = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

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

function Resolve-Python {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            & py -3.12 --version *> $null
            if ($LASTEXITCODE -eq 0) {
                return @("py", "-3.12")
            }
        }
        catch {
        }
        try {
            & py -3.11 --version *> $null
            if ($LASTEXITCODE -eq 0) {
                return @("py", "-3.11")
            }
        }
        catch {
        }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @("python")
    }

    throw "Python 3.11+ was not found on PATH. Install Python, then rerun this script."
}

Push-Location $ProjectRoot
try {
    if ($RecreateVenv -and (Test-Path $VenvPath)) {
        Remove-Item -LiteralPath $VenvPath -Recurse -Force
    }

    if (-not (Test-Path $VenvPython)) {
        $pythonCommand = Resolve-Python
        $pythonExe = $pythonCommand[0]
        $pythonArgs = @()
        if ($pythonCommand.Length -gt 1) {
            $pythonArgs += $pythonCommand[1..($pythonCommand.Length - 1)]
        }
        Invoke-Checked $pythonExe ($pythonArgs + @("-m", "venv", ".venv"))
    }

    Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-Checked $VenvPython @("-m", "pip", "install", "-e", ".")

    if (-not $SkipAgent) {
        Invoke-Checked $VenvPython @("-m", "causality.cli", "install-agent", "--project", ".")
    }

    if (-not $SkipDoctor) {
        & (Join-Path $PSScriptRoot "doctor.ps1")
    }
}
finally {
    Pop-Location
}
