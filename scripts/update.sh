#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
ALLOW_DIRTY=0
SKIP_TESTS=0
REFRESH_AGENT=0

for arg in "$@"; do
  case "$arg" in
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --refresh-agent) REFRESH_AGENT=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

cd "$PROJECT_ROOT"

if [[ "$ALLOW_DIRTY" -ne 1 && -n "$(git status --porcelain)" ]]; then
  echo "Working tree has local changes. Commit/stash them or rerun with --allow-dirty." >&2
  exit 1
fi

BRANCH="$(git branch --show-current)"
if [[ -z "$BRANCH" ]]; then
  echo "Detached HEAD is not supported by update.sh." >&2
  exit 1
fi

git fetch origin
git pull --ff-only origin "$BRANCH"

if [[ ! -x "$VENV_PYTHON" ]]; then
  bash "$PROJECT_ROOT/scripts/install.sh"
else
  "$VENV_PYTHON" -m pip install -e .
fi

if [[ "$REFRESH_AGENT" -eq 1 ]]; then
  "$VENV_PYTHON" -m causality.cli install-agent --project . --force
else
  "$VENV_PYTHON" -m causality.cli install-agent --project .
fi

if [[ "$SKIP_TESTS" -ne 1 ]]; then
  bash "$PROJECT_ROOT/scripts/doctor.sh"
fi
