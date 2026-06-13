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

    def test_latest_hash_matches_last_event_and_survives_reload(self) -> None:
        # R2: latest_hash() is served from a size-guarded cache, but must equal
        # the last appended event's hash, and a fresh instance on the same file
        # must read the same value from disk.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.append(AuditEventType.EVIDENCE, {"kind": "test_output"})
            last = ledger.append(AuditEventType.VERIFIER_DECISION, {"status": "pass"})

            self.assertEqual(ledger.latest_hash(), last.entry_hash)

            reloaded = EvidenceLedger(path)
            self.assertEqual(reloaded.latest_hash(), last.entry_hash)
            self.assertEqual(len(reloaded.events()), 2)
            # A further append on the fresh instance chains onto the disk tail.
            third = reloaded.append(AuditEventType.EVIDENCE, {"kind": "more"})
            self.assertEqual(third.previous_hash, last.entry_hash)
            self.assertTrue(reloaded.verify_chain())

    def test_cache_invalidated_when_sibling_instance_appends(self) -> None:
        # codex r3407872680: a second EvidenceLedger on the same file in this
        # process (e.g. mcp_server's long-lived ledger vs install_agent_files')
        # must not be served a stale latest_hash from its cache -- otherwise a
        # later append on the first instance would break the hash chain. The
        # size guard forces a re-read.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            a = EvidenceLedger(path)
            b = EvidenceLedger(path)

            first = a.append(AuditEventType.EVIDENCE, {"kind": "test_output"})
            # b primes its cache from disk (sees `first`).
            self.assertEqual(b.latest_hash(), first.entry_hash)
            # a appends again; b's cache is now behind by one row.
            second = a.append(AuditEventType.VERIFIER_DECISION, {"status": "pass"})

            # b must observe a's newer append, not its stale cache.
            self.assertEqual(b.latest_hash(), second.entry_hash)
            self.assertEqual(len(b.events()), 2)
            # ...and chain a fresh append onto the real tail.
            third = b.append(AuditEventType.EVIDENCE, {"kind": "more"})
            self.assertEqual(third.previous_hash, second.entry_hash)
            self.assertTrue(b.verify_chain())

    def test_events_for_contract_scopes_and_orders(self) -> None:
        # R2: contract-scoped accessors replace the hand-rolled
        # `event.contract_id == ...` filter that callers used over a full read.
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            a1 = ledger.append(AuditEventType.EVIDENCE, {"n": 1}, contract_id="A")
            ledger.append(AuditEventType.EVIDENCE, {"n": 2}, contract_id="B")
            a2 = ledger.append(AuditEventType.VERIFIER_DECISION, {"n": 3}, contract_id="A")

            a_events = ledger.events_for_contract("A")
            self.assertEqual([e.entry_hash for e in a_events], [a1.entry_hash, a2.entry_hash])
            self.assertEqual(ledger.latest_hash_for_contract("A"), a2.entry_hash)
            # B's latest differs from the global latest (A appended last).
            self.assertNotEqual(ledger.latest_hash_for_contract("B"), ledger.latest_hash())
            # Unknown contract: empty / None, never a cross-contract leak.
            self.assertEqual(ledger.events_for_contract("missing"), [])
            self.assertIsNone(ledger.latest_hash_for_contract("missing"))

    def test_mutating_returned_event_does_not_corrupt_ledger(self) -> None:
        # codex r3407872681: events() returns freshly parsed events, so mutating
        # a returned event's payload must not change what a later read or
        # verify_chain() sees (the prior cache shared mutable payload dicts).
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            ledger.append(AuditEventType.EVIDENCE, {"kind": "test_output"})

            got = ledger.events()
            got[0].payload["injected"] = "tampered"
            got.clear()

            fresh = ledger.events()
            self.assertEqual(len(fresh), 1)
            self.assertNotIn("injected", fresh[0].payload)
            self.assertTrue(ledger.verify_chain())

    def test_tail_zero_returns_empty(self) -> None:
        # Regression H5: tail(0) used to be events()[-0:] == the whole ledger.
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            ledger.append(AuditEventType.EVIDENCE, {"kind": "test_output"})
            ledger.append(AuditEventType.VERIFIER_DECISION, {"status": "pass"})

            self.assertEqual(ledger.tail(0), [])
            self.assertEqual(len(ledger.tail(1)), 1)
            self.assertEqual(len(ledger.tail(5)), 2)


if __name__ == "__main__":
    unittest.main()
