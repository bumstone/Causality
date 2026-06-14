from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality import MemoryGovernanceError, TypedMemory


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


class TtlEnforcementTests(unittest.TestCase):
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

    def test_sweep_and_revoke_reject_unknown_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            mem = TypedMemory(Path(temp_dir))
            with self.assertRaises(MemoryGovernanceError):
                mem.sweep("notes")
            with self.assertRaises(MemoryGovernanceError):
                mem.revoke("notes", "id")


if __name__ == "__main__":
    unittest.main()
