# BattleSpades one-line installer (Windows / PowerShell).
#   .\scripts\install.ps1
# Installs Python deps (incl. pyenet, compiled from source) and builds the
# Cython extensions. Requires: Python 3.8+, pip, and Visual Studio Build Tools
# ("Desktop development with C++": MSVC + Windows SDK).
$ErrorActionPreference = "Stop"

# cd to the project root (parent of this script)
Set-Location (Join-Path $PSScriptRoot "..")

# Prefer the `py` launcher, fall back to `python`.
$PY = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
Write-Host "==> Using interpreter: $(& $PY --version)"

Write-Host "==> Installing Python dependencies..."
& $PY -m pip install --upgrade pip
& $PY -m pip install -r requirements.txt

Write-Host "==> Building Cython extensions..."
& $PY setup.py build_ext --inplace

Write-Host ""
Write-Host "==> Done. Start the server with:"
Write-Host "      $PY run_server.py"
