$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw ".venv not found. Create it and install deps first (see README): python -m venv .venv; .\.venv\Scripts\python -m pip install -e '.[dev]'"
}
Set-Location -LiteralPath $root

$frontend = Join-Path $root "frontend"
$packageJson = Join-Path $frontend "package.json"
if (Test-Path -LiteralPath $packageJson) {
    Push-Location -LiteralPath $frontend
    try {
        Write-Host "Building frontend assets..."
        & pnpm build
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend build failed. Fix the pnpm build errors before starting the backend."
        }
    }
    finally {
        Pop-Location
    }
}

& $python -m backend.main

