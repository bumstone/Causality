#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"

cd "$PROJECT_ROOT"

if [[ "${1:-}" == "--recreate-venv" && -d .venv ]]; then
  rm -rf .venv
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
  "$PYTHON" -m venv .venv
fi

"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -e .
"$VENV_PYTHON" -m causality.cli install-agent --project .
"$PROJECT_ROOT/scripts/doctor.sh"
