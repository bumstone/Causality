"""Strict secret-free checkpoint contract for automatic orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from .contracts import utc_now
from .durable import file_lock, write_text_durably
from .task_lifecycle import canonical_sha256


_HASH = "0123456789abcdef"
_FIELDS = {
    "schema_version", "controller_id", "lease_id", "task_id", "phase_id",
    "operation", "idempotency_key", "request_sha256", "last_event_hash",
    "status", "updated_at",
}
_STATUSES = {"prepared", "acknowledged", "human_required"}


class OrchestrationError(ValueError):
    pass


def semantic_request_sha256(name: str, arguments: Mapping[str, Any]) -> str:
    """Hash a call without retaining or binding ephemeral approval proof."""

    safe = {key: value for key, value in arguments.items() if key != "proof"}
    return canonical_sha256({"tool": name, "arguments": safe})


@dataclass(frozen=True)
class OrchestrationCheckpoint:
    controller_id: str
    operation: str
    idempotency_key: str
    request_sha256: str
    status: str
    task_id: str | None = None
    lease_id: str | None = None
    phase_id: str | None = None
    last_event_hash: str | None = None
    updated_at: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise OrchestrationError("unsupported checkpoint state")
        if not isinstance(self.status, str) or self.status not in _STATUSES:
            raise OrchestrationError("unsupported checkpoint state")
        for name in ("controller_id", "operation", "idempotency_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise OrchestrationError(f"checkpoint {name} must be non-blank")
        if (
            not isinstance(self.request_sha256, str)
            or len(self.request_sha256) != 64
            or any(char not in _HASH for char in self.request_sha256)
        ):
            raise OrchestrationError("checkpoint request_sha256 must be a SHA-256")
        if self.last_event_hash is not None and (
                not isinstance(self.last_event_hash, str)
                or len(self.last_event_hash) != 64
                or any(char not in _HASH for char in self.last_event_hash)
        ):
            raise OrchestrationError("checkpoint last_event_hash must be a SHA-256")
        for name in ("task_id", "lease_id", "phase_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise OrchestrationError(f"checkpoint {name} must be null or non-blank")
        if not isinstance(self.updated_at, str):
            raise OrchestrationError("checkpoint updated_at must be text")
        if self.updated_at:
            try:
                parsed = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise OrchestrationError("checkpoint updated_at must be ISO-8601") from exc
            if parsed.utcoffset() is None:
                raise OrchestrationError("checkpoint updated_at must include a timezone")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "controller_id": self.controller_id,
            "lease_id": self.lease_id,
            "task_id": self.task_id,
            "phase_id": self.phase_id,
            "operation": self.operation,
            "idempotency_key": self.idempotency_key,
            "request_sha256": self.request_sha256,
            "last_event_hash": self.last_event_hash,
            "status": self.status,
            "updated_at": self.updated_at or utc_now(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OrchestrationCheckpoint":
        if set(value) != _FIELDS:
            raise OrchestrationError("checkpoint schema is not closed")
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise OrchestrationError("checkpoint field types are invalid") from exc


class CheckpointStore:
    def __init__(self, project: str | Path, controller_id: str):
        self.project = Path(project).resolve()
        if not isinstance(controller_id, str) or not controller_id.strip():
            raise OrchestrationError("controller_id must be non-blank")
        self.controller_id = controller_id
        filename = hashlib.sha256(controller_id.encode("utf-8")).hexdigest() + ".json"
        self.path = self.project / ".causality" / "orchestration" / filename
        self._assert_safe_path()

    def _assert_safe_path(self) -> None:
        if not self.path.is_relative_to(self.project):
            raise OrchestrationError("checkpoint path escapes the project")
        current = self.project
        for part in self.path.relative_to(self.project).parts:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            attributes = getattr(info, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if current.is_symlink() or (os.name == "nt" and attributes & reparse):
                raise OrchestrationError("checkpoint path contains a symlink or reparse point")
            if not current.resolve(strict=False).is_relative_to(self.project):
                raise OrchestrationError("checkpoint path resolves outside the project")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize read/prepare/call/ack for one controller across processes."""

        self._assert_safe_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_safe_path()
        with file_lock(self.path):
            self._assert_safe_path()
            yield

    def load(self) -> OrchestrationCheckpoint | None:
        self._assert_safe_path()
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OrchestrationError("checkpoint is unreadable") from exc
        if not isinstance(raw, dict):
            raise OrchestrationError("checkpoint must be an object")
        checkpoint = OrchestrationCheckpoint.from_mapping(raw)
        if checkpoint.controller_id != self.controller_id:
            raise OrchestrationError("checkpoint controller does not match its path")
        return checkpoint

    def save(self, checkpoint: OrchestrationCheckpoint) -> None:
        if checkpoint.controller_id != self.controller_id:
            raise OrchestrationError("checkpoint controller mismatch")
        self._assert_safe_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_safe_path()
        write_text_durably(
            self.path,
            json.dumps(checkpoint.to_dict(), ensure_ascii=True, sort_keys=True) + "\n",
        )

    def compare_and_save(
        self,
        expected: OrchestrationCheckpoint | None,
        checkpoint: OrchestrationCheckpoint,
    ) -> None:
        """Save only if the durable checkpoint still equals the caller's snapshot."""

        with self.transaction():
            if self.load() != expected:
                raise OrchestrationError("checkpoint changed concurrently")
            self.save(checkpoint)


__all__ = [
    "CheckpointStore", "OrchestrationCheckpoint", "OrchestrationError",
    "semantic_request_sha256",
]
