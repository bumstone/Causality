"""Typed long-term memory with pollution-prevention governance (ADR 0005).

Six typed stores keep tentative ``assumptions`` separate from confirmed
``decisions``. An assumption becomes a decision only through an explicit
promotion gate that requires confirming evidence, so a temporary judgement
cannot be reused as durable knowledge ("approved-once != true-forever"). Every
entry carries a ``provenance`` ref (e.g. a ledger ``entry_hash``) so its chain
of custody back to raw evidence survives the L0 boundary.

Entries are appended as JSONL under ``<root>/memory/<type>/log.jsonl`` so the
store mirrors the on-demand layout the bootstrap installs (ADR 0007).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import utc_now

MEMORY_TYPES = (
    "decisions",
    "assumptions",
    "failures",
    "playbooks",
    "snippets",
    "retrospectives",
)


class MemoryGovernanceError(ValueError):
    """Raised when a write would violate the typed-memory governance rules."""


@dataclass(frozen=True)
class MemoryEntry:
    type: str
    summary: str
    provenance: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "summary": self.summary,
            "provenance": self.provenance,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryEntry":
        return cls(
            type=value["type"],
            summary=value["summary"],
            provenance=value.get("provenance"),
            metadata=dict(value.get("metadata", {})),
            created_at=value.get("created_at", ""),
        )


@dataclass
class TypedMemory:
    """Append-only typed memory rooted at ``<root>/memory/``."""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def _log_path(self, mem_type: str) -> Path:
        return self.root / "memory" / mem_type / "log.jsonl"

    def record(self, mem_type: str, summary: str, *, provenance: str | None = None, **metadata: Any) -> MemoryEntry:
        """Append a typed entry.

        ``decisions`` cannot be written here: a decision must come through
        :meth:`promote_to_decision` so it always carries confirming evidence.
        """
        if mem_type not in MEMORY_TYPES:
            raise MemoryGovernanceError(f"unknown memory type: {mem_type!r}")
        if mem_type == "decisions":
            raise MemoryGovernanceError(
                "decisions are write-only via promote_to_decision (evidence required)"
            )
        return self._append(mem_type, summary, provenance, metadata)

    def note_assumption(
        self,
        summary: str,
        *,
        provenance: str | None = None,
        ttl_days: int | None = None,
    ) -> MemoryEntry:
        metadata: dict[str, Any] = {"status": "tentative"}
        if ttl_days is not None:
            metadata["ttl_days"] = ttl_days
        return self._append("assumptions", summary, provenance, metadata)

    def promote_to_decision(self, summary: str, *, evidence_ref: str) -> MemoryEntry:
        """Promote an assumption to a confirmed decision.

        The promotion gate (ADR 0005 §2.5): a decision may only be recorded with
        a non-empty confirming ``evidence_ref``.
        """
        if not evidence_ref or not str(evidence_ref).strip():
            raise MemoryGovernanceError(
                "assumption -> decision promotion requires a confirming evidence_ref"
            )
        return self._append("decisions", summary, evidence_ref, {"promoted_from": "assumption"})

    def record_failure(
        self,
        summary: str,
        *,
        scope: str,
        provenance: str | None = None,
        ttl_days: int | None = None,
        confidence: float | None = None,
    ) -> MemoryEntry:
        """Record a failure case with scope so guardrails do not become a
        permanent ratchet (ADR 0005 §2.5)."""
        if not scope or not scope.strip():
            raise MemoryGovernanceError("a failure must declare a scope")
        metadata: dict[str, Any] = {"scope": scope}
        if ttl_days is not None:
            metadata["ttl_days"] = ttl_days
        if confidence is not None:
            metadata["confidence"] = confidence
        return self._append("failures", summary, provenance, metadata)

    def entries(self, mem_type: str) -> list[MemoryEntry]:
        if mem_type not in MEMORY_TYPES:
            raise MemoryGovernanceError(f"unknown memory type: {mem_type!r}")
        path = self._log_path(mem_type)
        if not path.exists():
            return []
        records: list[MemoryEntry] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(MemoryEntry.from_dict(json.loads(line)))
        return records

    def _append(
        self,
        mem_type: str,
        summary: str,
        provenance: str | None,
        metadata: dict[str, Any],
    ) -> MemoryEntry:
        summary = summary.strip()
        if not summary:
            raise MemoryGovernanceError("summary is required")
        entry = MemoryEntry(
            type=mem_type,
            summary=summary,
            provenance=provenance,
            metadata=dict(metadata),
        )
        path = self._log_path(mem_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict(), ensure_ascii=True) + "\n")
        return entry
