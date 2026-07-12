from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality import MemoryGovernanceError, TypedMemory
from causality.durable import DurableJsonl


class TypedMemoryTests(unittest.TestCase):
    def test_decisions_cannot_be_written_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            with self.assertRaises(MemoryGovernanceError):
                mem.record("decisions", "we decided X")

    def test_promotion_requires_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            with self.assertRaises(MemoryGovernanceError):
                mem.promote_to_decision("X is true", evidence_ref="")

    def test_assumption_promotes_to_decision_with_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            mem.note_assumption("API returns UTC", ttl_days=30)
            entry = mem.promote_to_decision("API returns UTC", evidence_ref="ledger:abc123")

            self.assertEqual(entry.type, "decisions")
            self.assertEqual(entry.provenance, "ledger:abc123")

            assumptions = mem.entries("assumptions")
            decisions = mem.entries("decisions")
            self.assertEqual(len(assumptions), 1)
            self.assertEqual(assumptions[0].metadata["status"], "tentative")
            self.assertEqual(assumptions[0].metadata["ttl_days"], 30)
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0].metadata["promoted_from"], "assumption")

    def test_failure_requires_scope_and_keeps_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            with self.assertRaises(MemoryGovernanceError):
                mem.record_failure("flaky network", scope="")

            mem.record_failure("flaky network", scope="task:checkout", ttl_days=7, confidence=0.4)
            failures = mem.entries("failures")
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0].metadata["scope"], "task:checkout")
            self.assertEqual(failures[0].metadata["ttl_days"], 7)

    def test_unknown_type_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            with self.assertRaises(MemoryGovernanceError):
                mem.record("notes", "oops")

    def test_provenance_persists_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            mem.record("snippets", "causality context", provenance="ledger:deadbeef")
            again = TypedMemory(Path(temp_dir))
            snippets = again.entries("snippets")
            self.assertEqual(snippets[0].provenance, "ledger:deadbeef")

    def test_record_once_reuses_identical_canonical_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            first = mem.record_once(
                "retrospectives",
                "completed task",
                entry_id="task-1-retrospective",
                created_at="2026-07-11T00:00:00+00:00",
                provenance="ledger:abc",
                details={"b": 2, "a": 1},
            )
            retried = TypedMemory(Path(temp_dir)).record_once(
                "retrospectives",
                "completed task",
                entry_id="task-1-retrospective",
                created_at="2026-07-11T00:00:00+00:00",
                provenance="ledger:abc",
                details={"a": 1, "b": 2},
            )

            self.assertEqual(retried, first)
            self.assertEqual(len(mem.entries("retrospectives")), 1)

    def test_record_once_rejects_id_reuse_with_different_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            mem.record_once(
                "retrospectives",
                "first result",
                entry_id="stable-id",
                created_at="2026-07-11T00:00:00+00:00",
            )

            with self.assertRaisesRegex(MemoryGovernanceError, "different content"):
                mem.record_once(
                    "retrospectives",
                    "changed result",
                    entry_id="stable-id",
                    created_at="2026-07-11T00:00:00+00:00",
                )
            self.assertEqual(len(mem.entries("retrospectives")), 1)

    def test_record_once_preserves_decision_governance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            with self.assertRaises(MemoryGovernanceError):
                mem.record_once(
                    "decisions",
                    "bypass promotion",
                    entry_id="decision-id",
                    created_at="2026-07-11T00:00:00+00:00",
                    provenance="ledger:abc",
                )

    def test_record_once_is_atomic_across_concurrent_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def write_once(_: int):
                return TypedMemory(root).record_once(
                    "retrospectives",
                    "one durable reflection",
                    entry_id="concurrent-id",
                    created_at="2026-07-11T00:00:00+00:00",
                    provenance="ledger:abc",
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(write_once, range(32)))

            self.assertEqual({entry.entry_id for entry in results}, {"concurrent-id"})
            self.assertEqual(len(TypedMemory(root).entries("retrospectives")), 1)

    def test_record_once_concurrent_id_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def compete(summary: str) -> str:
                try:
                    TypedMemory(root).record_once(
                        "retrospectives",
                        summary,
                        entry_id="contested-id",
                        created_at="2026-07-11T00:00:00+00:00",
                    )
                except MemoryGovernanceError:
                    return "conflict"
                return "written"

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(compete, ("first", "second")))

            self.assertCountEqual(outcomes, ["written", "conflict"])
            self.assertEqual(len(TypedMemory(root).entries("retrospectives")), 1)


class TtlEnforcementTests(unittest.TestCase):
    def _assert_concurrent_record_once_survives(
        self,
        mem: TypedMemory,
        mem_type: str,
        rewrite: Callable[[], object],
    ) -> None:
        """Force record_once between a rewriter's read and replace."""
        path = mem._log_path(mem_type)
        read_complete = threading.Event()
        writer_started = threading.Event()
        writer_reached_append = threading.Event()
        errors: list[BaseException] = []
        original_read = DurableJsonl.read_lines
        original_append = DurableJsonl.append

        def controlled_read(store: DurableJsonl) -> list[str]:
            lines = original_read(store)
            if (
                threading.current_thread().name == "memory-rewriter"
                and store.path == path
                and not read_complete.is_set()
            ):
                read_complete.set()
                if not writer_started.wait(2):
                    raise AssertionError("concurrent writer did not start")
                # Without one lock around read-filter-rewrite, the writer reaches
                # append here and its new record is then overwritten. With the
                # lock, it cannot enter append until the rewrite has completed.
                writer_reached_append.wait(0.5)
            return lines

        def controlled_append(
            store: DurableJsonl,
            line: str,
            *,
            lock: bool = True,
        ) -> None:
            if threading.current_thread().name == "memory-writer":
                writer_reached_append.set()
            original_append(store, line, lock=lock)

        def run_rewrite() -> None:
            try:
                rewrite()
            except BaseException as exc:
                errors.append(exc)

        def run_writer() -> None:
            try:
                if not read_complete.wait(2):
                    raise AssertionError("rewriter did not read the log")
                writer_started.set()
                TypedMemory(mem.root).record_once(
                    mem_type,
                    "concurrent durable entry",
                    entry_id=f"{mem_type}-concurrent",
                    created_at="2026-07-11T00:00:00+00:00",
                )
            except BaseException as exc:
                errors.append(exc)

        with (
            patch.object(DurableJsonl, "read_lines", controlled_read),
            patch.object(DurableJsonl, "append", controlled_append),
        ):
            rewriter = threading.Thread(target=run_rewrite, name="memory-rewriter")
            writer = threading.Thread(target=run_writer, name="memory-writer")
            rewriter.start()
            writer.start()
            rewriter.join(5)
            writer.join(5)

        self.assertFalse(rewriter.is_alive(), "rewriter deadlocked")
        self.assertFalse(writer.is_alive(), "writer deadlocked")
        self.assertEqual(errors, [])
        self.assertIn(
            "concurrent durable entry",
            [entry.summary for entry in mem.entries(mem_type)],
        )

    def test_active_only_hides_expired_but_disk_keeps_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            mem.note_assumption("temporary guess", ttl_days=7)
            created = datetime.fromisoformat(mem.entries("assumptions")[0].created_at)

            near = created + timedelta(days=1)
            past_ttl = created + timedelta(days=8)
            self.assertEqual(len(mem.entries("assumptions", active_only=True, now=near)), 1)
            self.assertEqual(len(mem.entries("assumptions", active_only=True, now=past_ttl)), 0)
            # active_only never mutates the log: the entry is still on disk.
            self.assertEqual(len(mem.entries("assumptions", now=past_ttl)), 1)

    def test_entry_without_ttl_never_expires(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            mem.record("snippets", "permanent note")
            far_future = datetime.now(timezone.utc) + timedelta(days=99999)
            self.assertEqual(len(mem.entries("snippets", active_only=True, now=far_future)), 1)

    def test_sweep_reclaims_expired_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            mem.record_failure("transient flake", scope="task:x", ttl_days=1)
            mem.record_failure("durable issue", scope="task:y")  # no ttl
            ttl_entry = next(e for e in mem.entries("failures") if e.metadata.get("ttl_days"))
            after_expiry = datetime.fromisoformat(ttl_entry.created_at) + timedelta(days=2)

            self.assertEqual(mem.sweep("failures", now=after_expiry), 1)
            remaining = mem.entries("failures")
            self.assertEqual([e.summary for e in remaining], ["durable issue"])
            # Reload from disk: the sweep was persisted.
            self.assertEqual(len(TypedMemory(Path(temp_dir)).entries("failures")), 1)
            # Idempotent: nothing left to reclaim.
            self.assertEqual(mem.sweep("failures", now=after_expiry), 0)

    def test_sweep_does_not_erase_concurrent_record_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            expired = mem.record_failure("expired", scope="task:x", ttl_days=1)
            after_expiry = datetime.fromisoformat(expired.created_at) + timedelta(days=2)

            self._assert_concurrent_record_once_survives(
                mem,
                "failures",
                lambda: mem.sweep("failures", now=after_expiry),
            )

    def test_revoke_removes_entry_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            mem.note_assumption("revoke me", ttl_days=30)
            mem.note_assumption("keep me", ttl_days=30)
            target = next(e for e in mem.entries("assumptions") if e.summary == "revoke me")

            self.assertTrue(mem.revoke("assumptions", target.entry_id))
            self.assertEqual([e.summary for e in mem.entries("assumptions")], ["keep me"])
            # Unknown id is a no-op returning False.
            self.assertFalse(mem.revoke("assumptions", "no-such-id"))

    def test_revoke_does_not_erase_concurrent_record_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            target = mem.record("snippets", "revoke me")

            self._assert_concurrent_record_once_survives(
                mem,
                "snippets",
                lambda: mem.revoke("snippets", target.entry_id),
            )

    def test_naive_now_is_treated_as_utc(self) -> None:
        # codex #15 P2: an injected naive `now` (e.g. datetime.utcnow()) must be
        # normalized to UTC, not raise TypeError against the tz-aware expiry.
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            mem.note_assumption("temporary guess", ttl_days=7)
            created = datetime.fromisoformat(mem.entries("assumptions")[0].created_at)
            naive_after = (created + timedelta(days=8)).replace(tzinfo=None)

            self.assertEqual(len(mem.entries("assumptions", active_only=True, now=naive_after)), 0)
            self.assertEqual(mem.sweep("assumptions", now=naive_after), 1)

    def test_sweep_and_revoke_reject_unknown_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            with self.assertRaises(MemoryGovernanceError):
                mem.sweep("notes")
            with self.assertRaises(MemoryGovernanceError):
                mem.revoke("notes", "id")


if __name__ == "__main__":
    unittest.main()
