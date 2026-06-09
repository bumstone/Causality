from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ouroboros_hitl import MemoryGovernanceError, TypedMemory


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
            mem.record("snippets", "ouroboros-hitl context", provenance="ledger:deadbeef")
            again = TypedMemory(Path(temp_dir))
            snippets = again.entries("snippets")
            self.assertEqual(snippets[0].provenance, "ledger:deadbeef")


if __name__ == "__main__":
    unittest.main()
