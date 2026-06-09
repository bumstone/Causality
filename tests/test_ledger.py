from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import AuditEventType
from causality.ledger import EvidenceLedger


class LedgerTests(unittest.TestCase):
    def test_append_and_verify_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            first = ledger.append(AuditEventType.EVIDENCE, {"kind": "test_output"})
            second = ledger.append(AuditEventType.VERIFIER_DECISION, {"status": "pass"})

            self.assertIsNone(first.previous_hash)
            self.assertEqual(second.previous_hash, first.entry_hash)
            self.assertTrue(ledger.verify_chain())
            self.assertEqual(len(ledger.events()), 2)

    def test_artifact_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir) / "report.txt"
            artifact.write_text("ok", encoding="utf-8")
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            event = ledger.append(
                AuditEventType.EVIDENCE,
                {"kind": "artifact_hash"},
                artifact_paths=[artifact],
            )

            self.assertEqual(event.artifacts[0]["bytes"], 2)
            self.assertIsNotNone(event.artifacts[0]["sha256"])


if __name__ == "__main__":
    unittest.main()
