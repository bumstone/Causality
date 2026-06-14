from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from .contracts import AuditEventType, utc_now
from .durable import DurableJsonl, file_lock


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    file_path = Path(path)
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    timestamp: str
    event_type: str
    payload: dict[str, Any]
    contract_id: str | None
    artifacts: list[dict[str, Any]]
    previous_hash: str | None
    entry_hash: str


class EvidenceLedger:
    """Append-only JSONL ledger with hash chaining.

    The ledger treats agent prose as claims and tool-observed output as evidence.
    Artifact paths are hashed when the files exist so reports and screenshots can
    be linked without inlining heavy content into the LLM context.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Cache ONLY the latest hash, guarded by the file size. append() needs
        # the previous hash to chain; recomputing it by scanning the whole file
        # every append made N appends cost O(N^2). The cache makes the common
        # single-writer case O(1) per append, while the size guard keeps it
        # correct when another EvidenceLedger instance for the same file appended
        # in this process (e.g. mcp_server holds a long-lived ledger while
        # install_agent_files appends through its own instance, codex r3407872680):
        # a size change invalidates the cache and we re-read the tail. The append
        # log only grows, so size strictly increases and is a reliable signal.
        # We deliberately do NOT cache parsed events: events()/find() re-read and
        # re-parse on each call, so callers can never mutate shared cached state
        # (codex r3407872681) and never observe a stale list. (Cross-process
        # concurrency still needs locking -- ADR 0011 R4.)
        self._cached_latest_hash: str | None = None
        self._synced_size = -1
        # All JSONL file moves go through one helper so R4b/R4c can add fsync,
        # atomic rewrite, and flock in a single place (ADR 0011 §2.2).
        self._store = DurableJsonl(self.path)

    def _current_size(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def _read_latest_hash_from_disk(self) -> str | None:
        # Via the store so a torn trailing line (crashed append) is ignored when
        # picking the chain anchor (ADR 0011 §2.2 R4b).
        lines = self._store.read_lines()
        return json.loads(lines[-1]).get("entry_hash") if lines else None

    def append(
        self,
        event_type: AuditEventType | str,
        payload: dict[str, Any],
        *,
        contract_id: str | None = None,
        artifact_paths: Iterable[str | Path] = (),
    ) -> LedgerEvent:
        event_type_value = event_type.value if isinstance(event_type, AuditEventType) else str(event_type)
        artifacts = [self._artifact_record(path) for path in artifact_paths]
        # Hold the store lock across read-latest-hash + append so a second writer
        # to the same file cannot read the same previous_hash and fork the chain
        # (ADR 0011 §2.2 R4c). The size guard re-reads the tail if another writer
        # appended since our last sync; the store append runs lock=False since we
        # already hold it.
        with file_lock(self.path):
            previous_hash = self.latest_hash()
            entry_without_hash = {
                "event_id": str(uuid4()),
                "timestamp": utc_now(),
                "event_type": event_type_value,
                "contract_id": contract_id,
                "payload": payload,
                "artifacts": artifacts,
                "previous_hash": previous_hash,
            }
            entry_hash = sha256_text(_stable_json(entry_without_hash))
            entry = dict(entry_without_hash)
            entry["entry_hash"] = entry_hash
            self._store.append(
                json.dumps(entry, ensure_ascii=True, sort_keys=True), lock=False
            )
            # Resync to the row we just wrote so the next append is O(1) and the
            # size guard stays consistent with what is on disk.
            self._cached_latest_hash = entry_hash
            self._synced_size = self._current_size()
        return LedgerEvent(**entry)

    def events(self) -> list[LedgerEvent]:
        return [LedgerEvent(**json.loads(line)) for line in self._store.read_lines()]

    def find(
        self,
        event_type: AuditEventType | str | None = None,
        predicate: Callable[[LedgerEvent], bool] | None = None,
    ) -> list[LedgerEvent]:
        event_type_value = None
        if event_type is not None:
            event_type_value = event_type.value if isinstance(event_type, AuditEventType) else str(event_type)
        matches = []
        for event in self.events():
            if event_type_value is not None and event.event_type != event_type_value:
                continue
            if predicate is not None and not predicate(event):
                continue
            matches.append(event)
        return matches

    def latest_hash(self) -> str | None:
        size = self._current_size()
        if size != self._synced_size:
            # First read, or another writer changed the file: recompute + resync.
            self._cached_latest_hash = self._read_latest_hash_from_disk()
            self._synced_size = size
        return self._cached_latest_hash

    def events_for_contract(self, contract_id: str | None) -> list[LedgerEvent]:
        """Return one contract's events, in order.

        Centralizes contract scoping so callers (Reflect, Skill distill) stop
        re-implementing ``event.contract_id == ...`` over a full ledger read.
        """
        return [event for event in self.events() if event.contract_id == contract_id]

    def latest_hash_for_contract(self, contract_id: str | None) -> str | None:
        """Latest entry hash for one contract -- its provenance anchor.

        Using this instead of :meth:`latest_hash` avoids the footgun where, in
        interleaved multi-contract runs, the global latest hash belongs to a
        different contract and breaks the audit trail (codex review r3382219479).
        """
        scoped = self.events_for_contract(contract_id)
        return scoped[-1].entry_hash if scoped else None

    def verify_chain(self) -> bool:
        previous_hash = None
        for event in self.events():
            entry = {
                "event_id": event.event_id,
                "timestamp": event.timestamp,
                "event_type": event.event_type,
                "contract_id": event.contract_id,
                "payload": event.payload,
                "artifacts": event.artifacts,
                "previous_hash": event.previous_hash,
            }
            if event.previous_hash != previous_hash:
                return False
            if sha256_text(_stable_json(entry)) != event.entry_hash:
                return False
            previous_hash = event.entry_hash
        return True

    def tail(self, limit: int = 5) -> list[dict[str, Any]]:
        # [-0:] would return the whole list, dumping the full ledger when a
        # caller asks for zero entries (code review 2026-06-13, H5).
        if limit <= 0:
            return []
        events = self.events()[-limit:]
        return [
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "timestamp": event.timestamp,
                "payload": event.payload,
            }
            for event in events
        ]

    @staticmethod
    def _artifact_record(path: str | Path) -> dict[str, Any]:
        artifact = Path(path)
        record: dict[str, Any] = {"path": str(artifact)}
        if artifact.is_file():
            record["sha256"] = sha256_file(artifact)
            record["bytes"] = artifact.stat().st_size
        else:
            record["sha256"] = None
            record["missing"] = True
        return record
