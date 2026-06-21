from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable
from uuid import uuid4

from .contracts import AuditEventType, utc_now
from .durable import DurableJsonl, file_lock, write_text_durably


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

    def events(self, *, all_segments: bool = False) -> list[LedgerEvent]:
        """Current segment's events; ``all_segments`` prepends sealed archives.

        After :meth:`rotate`, the recent history lives in the current segment and
        older history in ``<path>.1``, ``<path>.2``, ... ``all_segments=True``
        returns the full chain in order across them.
        """
        if all_segments:
            archived = self._archived_events()
            if archived:
                return [self._isolate(event) for event in archived + self._load_events()]
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
            if lines:
                self._cached_latest_hash = json.loads(lines[-1]).get("entry_hash")
            else:
                # Empty current segment: chain onto the carry-over tail from a
                # prior rotation (if any), so the chain continues across the seam
                # instead of forking a new genesis (ADR 0011 R4f rotation).
                self._cached_latest_hash = self._carry_over_head()
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
        # r3445873874). Across rotation, verify the WHOLE chain (sealed archive
        # segments + the current one) -- the only correct integrity check, since
        # the current segment alone is not a standalone chain after a rotation:
        # its first entry's previous_hash points at the sealed tail. So the seam
        # is checked, and a tampered archive fails verification too.
        previous_hash = None
        for segment in self._archive_segments() + [self.path]:
            for line in DurableJsonl(segment).read_lines():
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

    # --- Rotation (ADR 0011 §3): seal a large ledger into archive segments while
    # keeping the hash chain verifiable across the seam. The mechanism is opt-in
    # (callers decide the policy via rotate()/maybe_rotate()); append() is
    # unchanged, so a non-rotating deployment pays nothing.

    def _head_path(self) -> Path:
        return Path(str(self.path) + ".head")

    def _carry_over_head(self) -> str | None:
        """The sealed tail hash a fresh current segment must chain onto."""
        try:
            text = self._head_path().read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return text or None

    def _archive_segments(self) -> list[Path]:
        """Sealed archives ``<path>.1``, ``<path>.2``, ... in chain order."""
        segments: list[Path] = []
        sequence = 1
        while True:
            segment = self.path.parent / f"{self.path.name}.{sequence}"
            if not segment.exists():
                break
            segments.append(segment)
            sequence += 1
        return segments

    def _archived_events(self) -> list[LedgerEvent]:
        events: list[LedgerEvent] = []
        for segment in self._archive_segments():
            events.extend(
                LedgerEvent(**json.loads(line)) for line in DurableJsonl(segment).read_lines()
            )
        return events

    def _reset_caches(self) -> None:
        self._cached_latest_hash = None
        self._synced_size = -1
        self._cached_events = None
        self._events_synced_size = -1

    def rotate(self) -> Path | None:
        """Seal the current ledger into an archive segment and start a fresh one.

        The chain continues across the seam: the current tail hash is persisted to
        a ``<path>.head`` sidecar so the next append (even from a fresh instance)
        chains onto the sealed segment's last entry, and
        ``verify_chain(all_segments=True)`` checks the whole chain. Returns the
        archive path, or ``None`` if the ledger is empty (nothing to seal).
        """
        with file_lock(self.path):
            tail = self.latest_hash()
            if tail is None:
                return None
            sequence = len(self._archive_segments()) + 1
            archive = self.path.parent / f"{self.path.name}.{sequence}"
            os.replace(self.path, archive)
            # Build the offset index for the sealed segment so event_count() and
            # events_page() can count/seek it without re-parsing the whole file.
            self._build_index(archive)
            # Persist the carry-over BEFORE returning so a crash right after the
            # rename cannot lose the chain anchor (the next append would otherwise
            # start a new genesis and fork the chain).
            write_text_durably(self._head_path(), tail + "\n", lock=False)
            self._reset_caches()
        return archive

    def maybe_rotate(self, *, max_bytes: int) -> Path | None:
        """Rotate when the current segment is at/over ``max_bytes``; else no-op."""
        if max_bytes > 0 and self._current_size() >= max_bytes:
            return self.rotate()
        return None

    # --- Offset index (ADR 0011 §3 `.idx`): a per-segment sidecar of byte
    # offsets so event_count()/events_page() can count and seek across archives
    # without parsing every line. The index is advisory -- if it is missing or
    # stale the methods fall back to a full parse, so correctness never depends
    # on it (only speed).

    def _index_path(self, segment: Path) -> Path:
        return Path(str(segment) + ".idx")

    def _build_index(self, segment: Path) -> dict[str, Any]:
        """Write ``<segment>.idx``: byte offset of each complete, non-blank line."""
        offsets: list[int] = []
        position = 0
        with segment.open("rb") as handle:
            for raw in handle:
                if raw.endswith(b"\n") and raw.strip():
                    offsets.append(position)
                position += len(raw)
        index = {"count": len(offsets), "offsets": offsets}
        write_text_durably(self._index_path(segment), json.dumps(index), lock=False)
        return index

    def _read_index(self, segment: Path) -> dict[str, Any] | None:
        try:
            return json.loads(self._index_path(segment).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _segment_count(self, segment: Path) -> int:
        index = self._read_index(segment)
        if index is not None:
            return int(index["count"])
        return len(DurableJsonl(segment).read_lines())

    def _segment_lines(self, segment: Path, start: int, limit: int) -> list[str]:
        """Lines ``[start:start+limit]`` of a segment, via the index when present."""
        index = self._read_index(segment)
        if index is None:
            return DurableJsonl(segment).read_lines()[start : start + limit]
        offsets = index["offsets"]
        end = min(start + limit, len(offsets))
        lines: list[str] = []
        with segment.open("rb") as handle:
            for i in range(start, end):
                handle.seek(offsets[i])
                line = handle.readline().decode("utf-8").strip()
                if line:
                    lines.append(line)
        return lines

    def _segments(self, *, all_segments: bool) -> list[Path]:
        return (self._archive_segments() if all_segments else []) + [self.path]

    def event_count(self, *, all_segments: bool = True) -> int:
        """Number of events across segments -- O(segments) via the index."""
        return sum(self._segment_count(seg) for seg in self._segments(all_segments=all_segments))

    def events_page(
        self, start: int, limit: int, *, all_segments: bool = True
    ) -> list[LedgerEvent]:
        """A window of events ``[start:start+limit]`` in chain order.

        Skips whole segments before ``start`` using their indexed counts and
        seeks into the target segment via the offset index, so a page does not
        parse the full (possibly rotated) history.
        """
        if limit <= 0 or start < 0:
            return []
        to_skip, remaining, page = start, limit, []
        for segment in self._segments(all_segments=all_segments):
            if remaining <= 0:
                break
            count = self._segment_count(segment)
            if to_skip >= count:
                to_skip -= count
                continue
            lines = self._segment_lines(segment, to_skip, remaining)
            page.extend(LedgerEvent(**json.loads(line)) for line in lines)
            remaining -= len(lines)
            to_skip = 0
        return [self._isolate(event) for event in page]

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
