"""Whitelisted environment evidence for orchestration controller events."""

from __future__ import annotations

import importlib.metadata
import os
import platform
import stat
from pathlib import Path
from typing import Any

from .task_lifecycle import canonical_sha256


_MAX_GIT_METADATA_BYTES = 4096


def _read_small_text(path: Path) -> str | None:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_GIT_METADATA_BYTES:
            os.close(descriptor)
            return None
        try:
            raw = os.read(descriptor, _MAX_GIT_METADATA_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > _MAX_GIT_METADATA_BYTES:
            return None
        return raw.decode("utf-8").strip()
    except (OSError, UnicodeError, BlockingIOError):
        return None


def _git_head(root: Path) -> str | None:
    """Read a loose Git HEAD without invoking repository-configured helpers."""

    git_dir = root / ".git"
    if not git_dir.is_dir():
        return None
    resolved_root = root.resolve()
    resolved_git_dir = git_dir.resolve()
    try:
        resolved_git_dir.relative_to(resolved_root)
    except ValueError:
        return None
    head_path = (resolved_git_dir / "HEAD").resolve()
    try:
        head_path.relative_to(resolved_git_dir)
    except ValueError:
        return None
    value = _read_small_text(head_path)
    if value is None:
        return None
    if value.startswith("ref: "):
        ref = value[5:]
        if not ref.startswith("refs/") or ".." in ref or "\\" in ref:
            return None
        candidate = (resolved_git_dir / ref).resolve()
        try:
            candidate.relative_to(resolved_git_dir)
        except ValueError:
            return None
        value = _read_small_text(candidate)
    if value is not None and len(value) == 40 and all(
        char in "0123456789abcdefABCDEF" for char in value
    ):
        return value.lower()
    return None


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
    head = _git_head(root)
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
        # Dirty state cannot be derived safely without parsing the index and
        # worktree; keep it unknown rather than executing Git/configured helpers.
        "git": {"head": head, "dirty": None},
    }


__all__ = ["bounded_environment_snapshot"]
