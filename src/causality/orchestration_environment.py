"""Whitelisted environment evidence for orchestration controller events."""

from __future__ import annotations

import importlib.metadata
import platform
import subprocess
from pathlib import Path
from typing import Any

from .task_lifecycle import canonical_sha256


def bounded_environment_snapshot(
    project: str | Path,
    capabilities: tuple[str, ...],
    policy_digest: str,
) -> dict[str, Any]:
    """Return platform metadata without reading environment variable values."""

    root = Path(project).resolve()
    try:
        version = importlib.metadata.version("causality")
    except importlib.metadata.PackageNotFoundError:
        version = "source"
    head: str | None = None
    dirty: bool | None = None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, check=False, timeout=5,
        )
        candidate = completed.stdout.strip()
        if completed.returncode == 0 and len(candidate) == 40 and all(
            char in "0123456789abcdef" for char in candidate
        ):
            head = candidate
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True,
            text=True, check=False, timeout=5,
        )
        if status.returncode == 0:
            dirty = bool(status.stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass
    advertised = sorted(set(capabilities))
    return {
        "causality_version": version,
        "python_version": platform.python_version(),
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "capabilities": advertised,
        "capabilities_sha256": canonical_sha256(advertised),
        "policy_sha256": policy_digest,
        "git": {"head": head, "dirty": dirty},
    }


__all__ = ["bounded_environment_snapshot"]
