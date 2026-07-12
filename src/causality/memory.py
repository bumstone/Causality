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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts import utc_now
from .durable import DurableJsonl, file_lock

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
    # Stable identity so a specific entry can be revoked before its TTL. Entries
    # written before this field existed fall back to a fresh id on read (their
    # data is ephemeral runtime state, ADR 0008), so revoke targets new entries.
    entry_id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "summary": self.summary,
            "provenance": self.provenance,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "entry_id": self.entry_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryEntry":
        return cls(
            type=value["type"],
            summary=value["summary"],
            provenance=value.get("provenance"),
            metadata=dict(value.get("metadata", {})),
            created_at=value.get("created_at", ""),
            entry_id=value.get("entry_id") or uuid4().hex,
        )

    def expiry(self) -> datetime | None:
        """When this entry expires, or ``None`` if it carries no ``ttl_days``."""
        ttl_days = self.metadata.get("ttl_days")
        if ttl_days is None:
            return None
        created = _parse_timestamp(self.created_at)
        if created is None:
            return None
        return created + timedelta(days=float(ttl_days))

    def is_expired(self, now: datetime) -> bool:
        expiry = self.expiry()
        if expiry is None:
            return False
        # Normalize an injected naive `now` to UTC, the same way _parse_timestamp
        # treats naive stored timestamps -- otherwise comparing a naive `now`
        # against the tz-aware expiry raises TypeError (codex #15 r3408928746).
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now >= expiry


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    # Compare in UTC; treat a naive timestamp as UTC rather than raising.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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

    def record_once(
        self,
        mem_type: str,
        summary: str,
        *,
        entry_id: str,
        created_at: str,
        provenance: str | None = None,
        **metadata: Any,
    ) -> MemoryEntry:
        """Append a caller-identified entry exactly once.

        A retry with the same ``entry_id`` and canonical entry content returns
        the entry already on disk. Reusing the id for different content fails
        closed. The lookup and append share the type log's file lock, so separate
        :class:`TypedMemory` instances and concurrent callers cannot both append
        the same entry.

        ``entry_id`` and ``created_at`` are caller-supplied so a durable workflow
        can derive them before a crash and reproduce the identical write on
        retry. As with :meth:`record`, decisions cannot bypass the promotion
        gate.
        """
        if mem_type not in MEMORY_TYPES:
            raise MemoryGovernanceError(f"unknown memory type: {mem_type!r}")
        if mem_type == "decisions":
            raise MemoryGovernanceError(
                "decisions are write-only via promote_to_decision (evidence required)"
            )
        summary = summary.strip()
        if not summary:
            raise MemoryGovernanceError("summary is required")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise MemoryGovernanceError("entry_id is required")
        if not isinstance(created_at, str) or not created_at.strip():
            raise MemoryGovernanceError("created_at is required")

        candidate = MemoryEntry(
            type=mem_type,
            summary=summary,
            provenance=provenance,
            metadata=dict(metadata),
            created_at=created_at,
            entry_id=entry_id,
        )
        expected = _canonical_content(candidate.to_dict())
        path = self._log_path(mem_type)
        store = DurableJsonl(path)
        with file_lock(path):
            matches = []
            for line in store.read_lines():
                value = json.loads(line)
                if value.get("entry_id") == entry_id:
                    matches.append(value)
            if matches:
                if any(_canonical_content(value) != expected for value in matches):
                    raise MemoryGovernanceError(
                        f"entry_id {entry_id!r} already exists with different content"
                    )
                return MemoryEntry.from_dict(matches[0])
            store.append(json.dumps(candidate.to_dict(), ensure_ascii=True), lock=False)
        return candidate

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

    def entries(
        self,
        mem_type: str,
        *,
        active_only: bool = False,
        now: datetime | None = None,
    ) -> list[MemoryEntry]:
        """Return a type's entries; with ``active_only`` expired ones are hidden.

        ``active_only`` filters on read only -- expired entries stay on disk until
        :meth:`sweep` reclaims them, so the filter never mutates the log. ``now``
        is injectable for deterministic expiry (defaults to the current UTC time).
        """
        if mem_type not in MEMORY_TYPES:
            raise MemoryGovernanceError(f"unknown memory type: {mem_type!r}")
        store = DurableJsonl(self._log_path(mem_type))
        records = [MemoryEntry.from_dict(json.loads(line)) for line in store.read_lines()]
        if not active_only:
            return records
        moment = now or datetime.now(timezone.utc)
        return [entry for entry in records if not entry.is_expired(moment)]

    def revoke(self, mem_type: str, entry_id: str) -> bool:
        """Drop one entry early (before its TTL) by id. Returns whether it existed.

        The audit trail lives in the EvidenceLedger; this L0 memory log may be
        rewritten, so revoke removes the entry rather than tombstoning it.
        """
        if mem_type not in MEMORY_TYPES:
            raise MemoryGovernanceError(f"unknown memory type: {mem_type!r}")
        path = self._log_path(mem_type)
        with file_lock(path):
            records = self.entries(mem_type)
            kept = [entry for entry in records if entry.entry_id != entry_id]
            if len(kept) == len(records):
                return False
            self._rewrite(mem_type, kept, lock=False)
        return True

    def sweep(self, mem_type: str, *, now: datetime | None = None) -> int:
        """Reclaim expired entries from disk. Returns how many were removed."""
        if mem_type not in MEMORY_TYPES:
            raise MemoryGovernanceError(f"unknown memory type: {mem_type!r}")
        moment = now or datetime.now(timezone.utc)
        path = self._log_path(mem_type)
        with file_lock(path):
            records = self.entries(mem_type)
            kept = [entry for entry in records if not entry.is_expired(moment)]
            removed = len(records) - len(kept)
            if removed:
                self._rewrite(mem_type, kept, lock=False)
        return removed

    def _rewrite(
        self,
        mem_type: str,
        records: list[MemoryEntry],
        *,
        lock: bool = True,
    ) -> None:
        DurableJsonl(self._log_path(mem_type)).rewrite(
            (json.dumps(entry.to_dict(), ensure_ascii=True) for entry in records),
            lock=lock,
        )

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
        DurableJsonl(self._log_path(mem_type)).append(
            json.dumps(entry.to_dict(), ensure_ascii=True)
        )
        return entry


def _canonical_content(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
