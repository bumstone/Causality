from __future__ import annotations

import sys
import tempfile
import threading
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

    def test_concurrent_appends_keep_chain_valid(self) -> None:
        # R4c: separate EvidenceLedger instances on the same file appending at
        # once must serialize on the flock so no two reads see the same
        # previous_hash and fork the chain. After the storm the chain verifies
        # and every event is present exactly once.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"

            def worker() -> None:
                ledger = EvidenceLedger(path)
                for _ in range(20):
                    ledger.append(AuditEventType.EVIDENCE, {"kind": "concurrent"})

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            reader = EvidenceLedger(path)
            events = reader.events()
            self.assertEqual(len(events), 80)
            self.assertEqual(len({e.entry_hash for e in events}), 80)
            self.assertTrue(reader.verify_chain())

    def test_torn_tail_recovered_on_next_append(self) -> None:
        # R4b: a crashed append can leave a half-written final line. The next
        # append must drop that torn partial and chain onto the last complete
        # event, not merge bytes into it.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.append(AuditEventType.EVIDENCE, {"kind": "first"})
            second = ledger.append(AuditEventType.VERIFIER_DECISION, {"status": "pass"})
            # Simulate a crashed third append: a partial line with no newline.
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"event_id": "partial", "payload": {"k"')

            fresh = EvidenceLedger(path)
            self.assertEqual(len(fresh.events()), 2)
            self.assertEqual(fresh.latest_hash(), second.entry_hash)
            third = fresh.append(AuditEventType.EVIDENCE, {"kind": "after_crash"})
            self.assertEqual(third.previous_hash, second.entry_hash)
            self.assertEqual(len(fresh.events()), 3)
            self.assertTrue(fresh.verify_chain())

    def test_events_cache_reuses_parse_until_size_changes(self) -> None:
        # R4f: events()/find()/verify_chain() are served from a size-guarded
        # parsed-events cache. Repeated reads must not re-read+re-parse the file,
        # an append on this instance must keep the cache warm, and a sibling
        # instance's append must invalidate it through the size guard.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            a = EvidenceLedger(path)
            a.append(AuditEventType.EVIDENCE, {"n": 1})

            reads = {"count": 0}
            real_read = a._store.read_lines_with_torn

            def counting_read() -> tuple[list[str], bool]:
                reads["count"] += 1
                return real_read()

            a._store.read_lines_with_torn = counting_read  # type: ignore[assignment]

            # First read parses once; repeats reuse the cache (no extra reads).
            self.assertEqual(len(a.events()), 1)
            self.assertEqual(len(a.find()), 1)
            self.assertTrue(a.verify_chain())
            self.assertEqual(reads["count"], 1)

            # An append on this instance keeps the cache warm: the new row shows
            # up without re-reading the whole file.
            a.append(AuditEventType.VERIFIER_DECISION, {"n": 2})
            self.assertEqual(len(a.events()), 2)
            self.assertEqual(reads["count"], 1)

            # A sibling instance appends behind a's back: the size guard must
            # force a re-read so a never serves a stale two-event view.
            EvidenceLedger(path).append(AuditEventType.EVIDENCE, {"n": 3})
            self.assertEqual(len(a.events()), 3)
            self.assertEqual(reads["count"], 2)
            self.assertTrue(a.verify_chain())

    def test_warm_cache_returns_isolated_copies(self) -> None:
        # R4f keeps the mutation guarantee even when served from a warm cache:
        # tampering with events()/find() output (payload, artifacts, or the list
        # itself) must not corrupt a later read or verify_chain().
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir) / "a.txt"
            artifact.write_text("x", encoding="utf-8")
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            ledger.append(AuditEventType.EVIDENCE, {"k": "v"}, artifact_paths=[artifact])

            warm = ledger.events()  # warms the cache
            warm[0].payload["injected"] = "x"
            warm[0].artifacts[0]["path"] = "hacked"
            found = ledger.find(AuditEventType.EVIDENCE)
            found[0].payload["injected2"] = "y"
            found.append(found[0])

            fresh = ledger.events()
            self.assertEqual(len(fresh), 1)
            self.assertNotIn("injected", fresh[0].payload)
            self.assertNotIn("injected2", fresh[0].payload)
            self.assertEqual(fresh[0].artifacts[0]["path"], str(artifact))
            self.assertTrue(ledger.verify_chain())

    def test_warm_append_does_not_alias_caller_payload(self) -> None:
        # codex r3445774529: with the cache already warm, append() must cache an
        # isolated event, not one sharing the caller's payload dict / the
        # returned event -- else mutating either after append corrupts the cache.
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            ledger.events()  # warm the (empty) cache so append takes the warm path
            payload = {"k": "v"}
            returned = ledger.append(AuditEventType.EVIDENCE, payload)

            # Tamper via both handles the caller still holds after append.
            payload["injected"] = "x"
            returned.payload["injected2"] = "y"

            fresh = ledger.events()
            self.assertEqual(len(fresh), 1)
            self.assertNotIn("injected", fresh[0].payload)
            self.assertNotIn("injected2", fresh[0].payload)
            self.assertTrue(ledger.verify_chain())

    def test_tail_returns_isolated_payload(self) -> None:
        # codex r3445774531: tail() slices the shared cache, so mutating a
        # returned tail payload must not corrupt the ledger cache.
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            ledger.append(AuditEventType.EVIDENCE, {"k": "v"})

            ledger.tail(1)[0]["payload"]["injected"] = "x"

            self.assertNotIn("injected", ledger.events()[0].payload)
            self.assertNotIn("injected", ledger.tail(1)[0]["payload"])
            self.assertTrue(ledger.verify_chain())

    def test_find_predicate_cannot_corrupt_cache(self) -> None:
        # codex r3445798584: find() scans the shared cache, so the predicate must
        # receive an isolated copy -- a predicate that mutates the event it sees
        # must not corrupt the cache or break a later verify_chain().
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            ledger.append(AuditEventType.EVIDENCE, {"k": "v"})

            def tampering_predicate(event: object) -> bool:
                event.payload["injected"] = "x"  # type: ignore[attr-defined]
                return True

            ledger.find(predicate=tampering_predicate)

            self.assertNotIn("injected", ledger.events()[0].payload)
            self.assertTrue(ledger.verify_chain())

    def test_torn_tail_read_is_not_trusted_as_cache_key(self) -> None:
        # codex r3445819560: reading over a torn tail must not key the cache to
        # the stat size (which counts the dropped partial). Otherwise a sibling
        # that repairs the tail and appends a same-length record leaves the size
        # unchanged and the reader keeps serving the stale cached list.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            writer = EvidenceLedger(path)
            writer.append(AuditEventType.EVIDENCE, {"k": "first"})
            # Crashed third append: a half-written final line (no newline).
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"event_id": "partial"')

            reader = EvidenceLedger(path)
            self.assertEqual(len(reader.events()), 1)  # drops torn, caches first

            # Simulate the size collision: pin the reported size to the torn
            # size, then have a sibling repair the tail and append a real event.
            torn_size = reader._current_size()
            reader._current_size = lambda: torn_size  # type: ignore[assignment]
            writer.append(AuditEventType.VERIFIER_DECISION, {"k": "second"})

            # Despite the unchanged (pinned) size, the reader must re-read because
            # its previous read saw a torn tail -- not serve the stale [first].
            fresh = reader.events()
            self.assertEqual(len(fresh), 2)
            self.assertTrue(reader.verify_chain())

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
