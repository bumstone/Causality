from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from .contracts import AuditEventType, utc_now


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

    def append(
        self,
        event_type: AuditEventType | str,
        payload: dict[str, Any],
        *,
        contract_id: str | None = None,
        artifact_paths: Iterable[str | Path] = (),
    ) -> LedgerEvent:
        previous_hash = self.latest_hash()
        event_type_value = event_type.value if isinstance(event_type, AuditEventType) else str(event_type)
        artifacts = [self._artifact_record(path) for path in artifact_paths]
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
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n")
        return LedgerEvent(**entry)

    def events(self) -> list[LedgerEvent]:
        if not self.path.exists():
            return []
        result: list[LedgerEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                result.append(LedgerEvent(**json.loads(line)))
        return result

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
        latest = None
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    latest = json.loads(line)
        return latest.get("entry_hash") if latest else None

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
