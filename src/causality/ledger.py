from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, replace
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
        # Cache the latest hash, guarded by the file size. append() needs
        # the previous hash to chain; recomputing it by scanning the whole file
        # every append made N appends cost O(N^2). The cache makes the common
        # single-writer case O(1) per append, while the size guard keeps it
        # correct when another EvidenceLedger instance for the same file appended
        # in this process (e.g. mcp_server holds a long-lived ledger while
        # install_agent_files appends through its own instance, codex r3407872680):
        # a size change invalidates the cache and we re-read the tail. The append
        # log only grows, so size strictly increases and is a reliable signal.
        # R4f extends the same size guard to the PARSED events so events() and
        # find() stop re-reading + re-parsing the whole file on every call.
        # (verify_chain() deliberately still parses from disk -- it is an
        # integrity check, codex r3445873874.) The two original objections to
        # caching parsed events are
        # handled head-on: staleness across sibling instances is caught by the
        # size guard (a sibling append changes the size and forces a rebuild),
        # and the shared-mutable-state footgun (codex r3407872681) is closed by
        # handing callers isolated copies via _isolate() -- only read-only
        # internal scans touch the cache directly. (Cross-process concurrency
        # still needs locking -- ADR 0011 R4.)
        self._cached_latest_hash: str | None = None
        self._synced_size = -1
        self._cached_events: list[LedgerEvent] | None = None
        self._events_synced_size = -1
        # All JSONL file moves go through one helper so R4b/R4c can add fsync,
        # atomic rewrite, and flock in a single place (ADR 0011 §2.2).
        self._store = DurableJsonl(self.path)

    def _current_size(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

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
            # Capture the size the cache was synced at BEFORE we grow the file,
            # so we can tell whether the parsed-events cache is exactly one row
            # behind (safe to extend) or stale from a sibling write (rebuild).
            # The exact JSON row persisted to disk; reused for the warm-cache
            # entry below so a warm read parses the same bytes a cold read would.
            serialized = json.dumps(entry, ensure_ascii=True, sort_keys=True)
            pre_size = self._current_size()
            self._store.append(serialized, lock=False)
            # Resync to the row we just wrote so the next append is O(1) and the
            # size guard stays consistent with what is on disk.
            self._cached_latest_hash = entry_hash
            new_size = self._current_size()
            self._synced_size = new_size
            # Keep the parsed-events cache warm across append-then-read, but only
            # when it reflected the file right before this append; otherwise drop
            # it so _load_events() rebuilds under the size guard.
            if self._cached_events is not None and self._events_synced_size == pre_size:
                # Build the cached event from the persisted JSON row, not the
                # in-memory entry: json normalizes some payloads (tuple->array,
                # non-str keys->strings), so parsing the row keeps warm reads
                # identical to cold disk reads -- and yields fresh objects, so the
                # cache never aliases the caller's payload (codex r3445847631,
                # r3445774529).
                self._cached_events.append(LedgerEvent(**json.loads(serialized)))
                self._events_synced_size = new_size
            else:
                self._cached_events = None
                self._events_synced_size = -1
        return LedgerEvent(**entry)

    def _load_events(self) -> list[LedgerEvent]:
        # Size-guarded parsed-events cache (R4f). The append log only grows, so a
        # size change is the same reliable "someone wrote" signal R2 uses for
        # latest_hash. While the size is unchanged we reuse the parsed list
        # instead of re-reading + re-parsing the whole file on every call. The
        # returned list is the SHARED cache: callers that may hand events out
        # must go through events()/_isolate(); only read-only internal scans use
        # it directly.
        size = self._current_size()
        if self._cached_events is None or size != self._events_synced_size:
            lines, torn = self._store.read_lines_with_torn()
            self._cached_events = [LedgerEvent(**json.loads(line)) for line in lines]
            # A torn tail means stat size counts bytes read_lines just dropped; a
            # later repair+append of the same length would leave the size
            # unchanged and hide the new event. Don't trust the size then --
            # force a re-read next call (codex r3445819560).
            self._events_synced_size = -1 if torn else size
        return self._cached_events

    @staticmethod
    def _isolate(event: LedgerEvent) -> LedgerEvent:
        # Hand callers a copy whose mutable payload/artifacts cannot alias the
        # cache, so mutating events()/find() output never corrupts a later read
        # or verify_chain() (codex r3407872681 -- the regression that argued
        # against caching parsed events at all). Scalar fields are immutable.
        return replace(
            event,
            payload=copy.deepcopy(event.payload),
            artifacts=copy.deepcopy(event.artifacts),
        )

    def events(self) -> list[LedgerEvent]:
        return [self._isolate(event) for event in self._load_events()]

    def find(
        self,
        event_type: AuditEventType | str | None = None,
        predicate: Callable[[LedgerEvent], bool] | None = None,
    ) -> list[LedgerEvent]:
        event_type_value = None
        if event_type is not None:
            event_type_value = event_type.value if isinstance(event_type, AuditEventType) else str(event_type)
        matches = []
        # Iterate a snapshot (list(...)), not the live cache: a predicate that
        # appends to this same ledger extends _cached_events, which would
        # otherwise grow the active scan mid-loop (even unboundedly) and include
        # events that did not exist at scan start (codex r3445896987).
        for event in list(self._load_events()):
            if event_type_value is not None and event.event_type != event_type_value:
                continue
            # Isolate BEFORE the predicate: it is caller code that may mutate or
            # stash the event it receives, and running it against the shared
            # cache would let that corrupt later reads (codex r3445798584). The
            # cheap event_type filter runs first so only candidates are copied.
            candidate = self._isolate(event)
            if predicate is not None and not predicate(candidate):
                continue
            matches.append(candidate)
        return matches

    def latest_hash(self) -> str | None:
        size = self._current_size()
        if size != self._synced_size:
            # First read, or another writer changed the file: recompute + resync.
            # A torn trailing line is ignored when picking the chain anchor, so
            # don't key the cache to a stat size that counts those dropped bytes
            # either -- otherwise a same-length repair+append could serve a stale
            # anchor and fork the chain (codex r3445819560).
            lines, torn = self._store.read_lines_with_torn()
            self._cached_latest_hash = (
                json.loads(lines[-1]).get("entry_hash") if lines else None
            )
            self._synced_size = -1 if torn else size
        return self._cached_latest_hash

    def events_for_contract(self, contract_id: str | None) -> list[LedgerEvent]:
        """Return one contract's events, in order.

        Centralizes contract scoping so callers (Reflect, Skill distill) stop
        re-implementing ``event.contract_id == ...`` over a full ledger read.
        """
        return [
            self._isolate(event)
            for event in self._load_events()
            if event.contract_id == contract_id
        ]

    def latest_hash_for_contract(self, contract_id: str | None) -> str | None:
        """Latest entry hash for one contract -- its provenance anchor.

        Using this instead of :meth:`latest_hash` avoids the footgun where, in
        interleaved multi-contract runs, the global latest hash belongs to a
        different contract and breaks the audit trail (codex review r3382219479).
        """
        # entry_hash is an immutable str, so scan the shared cache directly
        # rather than materializing isolated copies just to read one field.
        latest = None
        for event in self._load_events():
            if event.contract_id == contract_id:
                latest = event.entry_hash
        return latest

    def verify_chain(self) -> bool:
        # Integrity check: read straight from disk, NOT the size-guarded cache.
        # A same-length in-place edit (e.g. flipping a payload byte without
        # fixing entry_hash) leaves the file size unchanged, so a cached scan
        # would still pass; re-parsing the persisted bytes catches it (codex
        # r3445873874).
        previous_hash = None
        for line in self._store.read_lines():
            event = LedgerEvent(**json.loads(line))
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
        events = self._load_events()[-limit:]
        return [
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "timestamp": event.timestamp,
                # Deep-copy: this slices the shared cache, so handing back the
                # cached payload dict raw would let a caller mutating a tail
                # result corrupt the ledger cache (codex r3445774531).
                "payload": copy.deepcopy(event.payload),
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
