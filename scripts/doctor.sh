#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"

cd "$PROJECT_ROOT"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Virtual environment is missing. Run scripts/install.sh first." >&2
  exit 1
fi

echo "== Git =="
git status --short --branch

echo
echo "== Python =="
"$VENV_PYTHON" --version
"$VENV_PYTHON" -m pip --version

echo
echo "== Causality CLI =="
"$VENV_PYTHON" -m causality.cli manifest --pretty
"$VENV_PYTHON" -m causality.cli context --pretty

echo
echo "== Tests =="
"$VENV_PYTHON" -m unittest discover -s tests

echo
echo "== Optional tools =="
if command -v node >/dev/null 2>&1; then node --version; else echo "node: not found"; fi
if command -v npm >/dev/null 2>&1; then npm --version; else echo "npm: not found"; fi
if command -v codex >/dev/null 2>&1; then codex --version; else echo "codex: not found"; fi
