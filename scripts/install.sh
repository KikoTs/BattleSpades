#!/usr/bin/env bash
# BattleSpades one-line installer (Linux / macOS).
#   ./scripts/install.sh
# Installs Python deps (incl. pyenet, compiled from source) and builds the
# Cython extensions. Requires: Python 3.8+, pip, and a C toolchain
#   - Debian/Ubuntu: sudo apt install build-essential python3-dev
#   - Fedora/RHEL:   sudo dnf install gcc python3-devel
#   - macOS:         xcode-select --install
set -euo pipefail

# cd to the project root (parent of this script)
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  PY=python
fi

echo "==> Using interpreter: $($PY --version 2>&1)"

echo "==> Checking for a C compiler..."
if ! command -v cc >/dev/null 2>&1 && ! command -v gcc >/dev/null 2>&1 && ! command -v clang >/dev/null 2>&1; then
  echo "!! No C compiler found. Install one first:" >&2
  echo "   Debian/Ubuntu: sudo apt install build-essential python3-dev" >&2
  echo "   Fedora/RHEL:   sudo dnf install gcc python3-devel" >&2
  echo "   macOS:         xcode-select --install" >&2
  exit 1
fi

echo "==> Installing Python dependencies..."
"$PY" -m pip install --upgrade pip
"$PY" -m pip install -r requirements.txt

echo "==> Building Cython extensions..."
"$PY" setup.py build_ext --inplace

echo ""
echo "==> Done. Start the server with:"
echo "      $PY run_server.py"
