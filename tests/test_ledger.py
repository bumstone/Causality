from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import AuditEventType
import causality.ledger as ledger_module
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

            # First read parses once; repeated events()/find() reuse the cache
            # (no extra reads). verify_chain() is excluded -- it reads disk by
            # design (integrity check, see test_verify_chain_reads_disk_not_cache).
            self.assertEqual(len(a.events()), 1)
            self.assertEqual(len(a.find()), 1)
            self.assertEqual(len(a.events()), 1)
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

    def test_warm_append_matches_disk_normalized_shape(self) -> None:
        # codex r3445847631: a warm-cache append must reflect the JSON-normalized
        # shape (e.g. tuple -> list) a cold disk read produces, not the caller's
        # in-memory payload, so warm and reloaded reads never diverge.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.events()  # warm the cache so append takes the warm path
            ledger.append(AuditEventType.EVIDENCE, {"items": (1, 2)})

            warm = ledger.events()[0].payload["items"]
            cold = EvidenceLedger(path).events()[0].payload["items"]
            self.assertEqual(warm, cold)
            self.assertEqual(warm, [1, 2])
            self.assertIsInstance(warm, list)
            self.assertTrue(ledger.verify_chain())

    def test_find_predicate_appends_do_not_affect_active_scan(self) -> None:
        # codex r3445896987: find() must iterate a snapshot, so a predicate that
        # appends to the same ledger cannot extend the list being scanned (which
        # could loop unboundedly) or include events that did not exist at start.
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            ledger.append(AuditEventType.EVIDENCE, {"n": 1})
            ledger.events()  # warm the cache so a mid-scan append extends it

            seen = {"count": 0}

            def appending_predicate(event: object) -> bool:
                seen["count"] += 1
                if seen["count"] <= 1:  # append once, from inside the scan
                    ledger.append(AuditEventType.EVIDENCE, {"n": 99})
                return True

            result = ledger.find(AuditEventType.EVIDENCE, predicate=appending_predicate)
            # The scan saw only the event present when find() started.
            self.assertEqual(seen["count"], 1)
            self.assertEqual(len(result), 1)
            # The append still landed and is visible to a later read.
            self.assertEqual(len(ledger.events()), 2)

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

    def test_context_tail_omits_raw_payload_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir) / "credential.txt"
            artifact.write_text("secret", encoding="utf-8")
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            first = ledger.append(
                AuditEventType.EVIDENCE,
                {"token": "context-sentinel", "kind": "test_output"},
                contract_id="private-contract-id",
                artifact_paths=(artifact,),
            )
            second = ledger.append(
                AuditEventType.STATE_TRANSITION,
                {"state": "executing"},
                contract_id="private-contract-id",
            )
            third = ledger.append(
                AuditEventType.EVIDENCE,
                {"kind": "other"},
                contract_id="another-private-id",
            )

            context = ledger.context_tail(3)

            self.assertEqual(
                [item["event_id"] for item in context],
                [first.event_id, second.event_id, third.event_id],
            )
            self.assertEqual(
                [item["contract_ref"] for item in context],
                ["contract-1", "contract-1", "contract-2"],
            )
            self.assertNotIn("context-sentinel", json.dumps(context))
            self.assertNotIn("private-contract-id", json.dumps(context))
            self.assertNotIn("another-private-id", json.dumps(context))

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

    def test_verify_chain_reads_disk_not_cache(self) -> None:
        # codex r3445873874: verify_chain() is an integrity check, so it must
        # read the persisted bytes, not the size-guarded cache. A same-length
        # in-place edit leaves the file size unchanged; a warmed instance must
        # still catch it.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.append(AuditEventType.EVIDENCE, {"k": "zzzzz"})
            self.assertTrue(ledger.verify_chain())  # warms the cache, valid

            # Tamper in place: same-length value swap leaves entry_hash stale and
            # the file size unchanged, so a size-guarded cache would miss it.
            raw = path.read_text(encoding="utf-8")
            self.assertIn("zzzzz", raw)
            tampered = raw.replace("zzzzz", "yyyyy")
            self.assertEqual(len(tampered), len(raw))
            path.write_text(tampered, encoding="utf-8")

            self.assertFalse(ledger.verify_chain())

    def test_rotate_seals_and_preserves_chain_across_seam(self) -> None:
        # Rotation seals the current segment into <path>.1 and starts a fresh one;
        # the hash chain continues across the seam (ADR 0011 §3).
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.append(AuditEventType.EVIDENCE, {"n": 1})
            ledger.append(AuditEventType.EVIDENCE, {"n": 2})
            sealed_tail = ledger.append(AuditEventType.VERIFIER_DECISION, {"n": 3})

            archive = ledger.rotate()
            self.assertEqual(archive, Path(str(path) + ".1"))
            self.assertTrue(archive.exists())
            self.assertFalse(path.exists())  # current segment sealed away

            self.assertEqual(ledger.events(), [])  # current segment is empty
            self.assertEqual(len(ledger.events(all_segments=True)), 3)
            self.assertEqual(ledger.latest_hash(), sealed_tail.entry_hash)  # carry-over

            # New appends chain across the seam onto the sealed tail.
            e4 = ledger.append(AuditEventType.EVIDENCE, {"n": 4})
            self.assertEqual(e4.previous_hash, sealed_tail.entry_hash)
            ledger.append(AuditEventType.EVIDENCE, {"n": 5})
            self.assertEqual(len(ledger.events()), 2)                    # current only
            self.assertEqual(len(ledger.events(all_segments=True)), 5)   # whole chain
            self.assertTrue(ledger.verify_chain())                       # verifies the seam

    def test_rotate_chain_survives_fresh_instance(self) -> None:
        # The carry-over is persisted, so a brand-new instance on the same path
        # continues the chain rather than forking a new genesis.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.append(AuditEventType.EVIDENCE, {"n": 1})
            last = ledger.append(AuditEventType.EVIDENCE, {"n": 2})
            ledger.rotate()

            fresh = EvidenceLedger(path)
            self.assertEqual(fresh.latest_hash(), last.entry_hash)
            third = fresh.append(AuditEventType.EVIDENCE, {"n": 3})
            self.assertEqual(third.previous_hash, last.entry_hash)
            self.assertTrue(fresh.verify_chain())
            self.assertEqual(len(fresh.events(all_segments=True)), 3)

    def test_rotate_twice_chains_across_all_segments(self) -> None:
        # Two rotations produce <path>.1 and <path>.2; the chain stays continuous
        # across BOTH seams and the full history is ordered.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.append(AuditEventType.EVIDENCE, {"n": 1})
            ledger.append(AuditEventType.EVIDENCE, {"n": 2})
            first = ledger.rotate()
            ledger.append(AuditEventType.EVIDENCE, {"n": 3})
            ledger.append(AuditEventType.EVIDENCE, {"n": 4})
            second = ledger.rotate()
            ledger.append(AuditEventType.EVIDENCE, {"n": 5})

            self.assertEqual(first, Path(str(path) + ".1"))
            self.assertEqual(second, Path(str(path) + ".2"))
            self.assertTrue(ledger.verify_chain())  # continuous across both seams
            ordered = [event.payload["n"] for event in ledger.events(all_segments=True)]
            self.assertEqual(ordered, [1, 2, 3, 4, 5])

    def test_rotate_empty_ledger_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            self.assertIsNone(ledger.rotate())

    def test_rotate_already_empty_current_segment_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            ledger.append(AuditEventType.EVIDENCE, {"n": 1})
            self.assertIsNotNone(ledger.rotate())
            self.assertIsNone(ledger.rotate())

    def test_cache_invalidates_on_rotate_and_same_size_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            stale = EvidenceLedger(path)
            stale.append(AuditEventType.EVIDENCE, {"n": 0})
            stale.rotate()
            first = stale.append(AuditEventType.EVIDENCE, {"n": 1})
            self.assertEqual(stale.events()[0].entry_hash, first.entry_hash)
            sibling = EvidenceLedger(path)
            sibling.rotate()
            second = sibling.append(AuditEventType.EVIDENCE, {"n": 2})
            self.assertEqual(path.stat().st_size, Path(str(path) + ".2").stat().st_size)

            self.assertEqual([event.payload["n"] for event in stale.events()], [2])
            self.assertEqual(
                [event.payload["n"] for event in stale.events(all_segments=True)],
                [0, 1, 2],
            )
            self.assertEqual(stale.latest_hash(), second.entry_hash)
            stale.append(AuditEventType.EVIDENCE, {"n": 3})
            self.assertTrue(stale.verify_chain())

    def test_pending_anchor_recovers_after_final_anchor_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            real_write = ledger_module.write_text_durably
            calls = 0

            def fail_final_anchor(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated final anchor failure")
                return real_write(*args, **kwargs)

            with mock.patch.object(
                ledger_module,
                "write_text_durably",
                side_effect=fail_final_anchor,
            ):
                with self.assertRaises(OSError):
                    ledger.append(AuditEventType.EVIDENCE, {"n": 1})

            self.assertTrue(ledger.verify_chain())
            self.assertEqual([event.payload["n"] for event in ledger.events()], [1])
            ledger.append(AuditEventType.EVIDENCE, {"n": 2})
            self.assertTrue(ledger.verify_chain())

    def test_pending_anchor_rolls_back_when_row_append_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            with mock.patch.object(
                ledger._store,
                "append",
                side_effect=OSError("simulated row failure"),
            ):
                with self.assertRaises(OSError):
                    ledger.append(AuditEventType.EVIDENCE, {"n": 1})

            self.assertTrue(ledger.verify_chain())
            self.assertEqual(ledger.events(), [])
            ledger.append(AuditEventType.EVIDENCE, {"n": 2})
            self.assertTrue(ledger.verify_chain())

    def test_missing_anchor_and_unrotated_suffix_truncation_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.append(AuditEventType.EVIDENCE, {"n": 1})
            ledger.append(AuditEventType.EVIDENCE, {"n": 2})
            Path(str(path) + ".head").unlink()
            first_row = path.read_text(encoding="utf-8").splitlines()[0]
            path.write_text(first_row + "\n", encoding="utf-8")

            self.assertFalse(ledger.verify_chain())

    def test_archive_numbering_gap_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.append(AuditEventType.EVIDENCE, {"n": 1})
            first = ledger.rotate()
            Path(str(path) + ".3").write_bytes(first.read_bytes())

            self.assertFalse(ledger.verify_chain())

    def test_legacy_rotation_failure_keeps_chain_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.append(AuditEventType.EVIDENCE, {"n": 1})

            legacy = json.loads(path.read_text(encoding="utf-8"))
            legacy.pop("anchor_version")
            unsigned = {key: value for key, value in legacy.items() if key != "entry_hash"}
            legacy["entry_hash"] = ledger_module.sha256_text(
                ledger_module._stable_json(unsigned)
            )
            path.write_text(
                json.dumps(legacy, ensure_ascii=True, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            Path(str(path) + ".head").unlink()
            self.assertTrue(ledger.verify_chain())

            with mock.patch.object(
                ledger,
                "_build_index",
                side_effect=OSError("simulated index failure"),
            ):
                with self.assertRaises(OSError):
                    ledger.rotate()

            self.assertTrue(ledger.verify_chain())
            follow_up = ledger.append(AuditEventType.EVIDENCE, {"n": 2})
            self.assertEqual(follow_up.previous_hash, legacy["entry_hash"])
            self.assertTrue(ledger.verify_chain())

    def test_corrupt_anchor_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.append(AuditEventType.EVIDENCE, {"n": 1})
            Path(str(path) + ".head").write_bytes(b"\xff")

            self.assertFalse(ledger.verify_chain())

    def test_unreadable_anchor_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.append(AuditEventType.EVIDENCE, {"n": 1})
            head = Path(str(path) + ".head")
            real_read_text = Path.read_text

            def fail_head_read(candidate: Path, *args, **kwargs):
                if candidate == head:
                    raise OSError("simulated unreadable anchor")
                return real_read_text(candidate, *args, **kwargs)

            with mock.patch.object(Path, "read_text", fail_head_read):
                self.assertFalse(ledger.verify_chain())

    def test_latest_contract_hash_survives_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            event = ledger.append(
                AuditEventType.EVIDENCE,
                {"kind": "test_output"},
                contract_id="task-a",
            )
            self.assertIsNotNone(ledger.rotate())

            self.assertEqual(ledger.latest_hash_for_contract("task-a"), event.entry_hash)

    def test_verify_chain_detects_tampered_archive(self) -> None:
        # A same-length in-place edit to a SEALED archive must fail the full
        # chain verification (the seam + archive content are checked).
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.append(AuditEventType.EVIDENCE, {"k": "zzzzz"})
            ledger.append(AuditEventType.EVIDENCE, {"k": "yyyyy"})
            archive = ledger.rotate()
            ledger.append(AuditEventType.EVIDENCE, {"k": "after"})
            self.assertTrue(ledger.verify_chain())

            raw = archive.read_text(encoding="utf-8")
            self.assertIn("zzzzz", raw)
            archive.write_text(raw.replace("zzzzz", "wwwww"), encoding="utf-8")
            self.assertFalse(ledger.verify_chain())

    def test_maybe_rotate_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.append(AuditEventType.EVIDENCE, {"n": 1})
            size = path.stat().st_size
            self.assertIsNone(ledger.maybe_rotate(max_bytes=size + 1000))  # below -> no-op
            self.assertTrue(path.exists())
            self.assertIsNotNone(ledger.maybe_rotate(max_bytes=size))  # at/over -> rotates
            self.assertFalse(path.exists())

    def _rotated_ledger(self, path: Path) -> "EvidenceLedger":
        # 2 events -> .1, 3 events -> .2, 1 event in the current segment (6 total).
        ledger = EvidenceLedger(path)
        for n in (1, 2):
            ledger.append(AuditEventType.EVIDENCE, {"n": n})
        ledger.rotate()
        for n in (3, 4, 5):
            ledger.append(AuditEventType.EVIDENCE, {"n": n})
        ledger.rotate()
        ledger.append(AuditEventType.EVIDENCE, {"n": 6})
        return ledger

    def test_rotate_builds_offset_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = EvidenceLedger(path)
            ledger.append(AuditEventType.EVIDENCE, {"n": 1})
            ledger.append(AuditEventType.EVIDENCE, {"n": 2})
            ledger.rotate()
            index_path = Path(str(path) + ".1.idx")
            self.assertTrue(index_path.exists())
            self.assertEqual(json.loads(index_path.read_text())["count"], 2)

    def test_event_count_across_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = self._rotated_ledger(Path(temp_dir) / "ledger.jsonl")
            self.assertEqual(ledger.event_count(), 6)              # archives + current
            self.assertEqual(ledger.event_count(all_segments=False), 1)  # current only

    def test_events_page_across_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = self._rotated_ledger(Path(temp_dir) / "ledger.jsonl")

            def ns(start, limit):
                return [e.payload["n"] for e in ledger.events_page(start, limit)]

            self.assertEqual(ns(0, 4), [1, 2, 3, 4])   # spans .1 and .2
            self.assertEqual(ns(4, 10), [5, 6])         # spans .2 and current
            self.assertEqual(ns(2, 2), [3, 4])          # window inside .2
            self.assertEqual(ns(10, 5), [])             # past the end
            self.assertEqual(ledger.events_page(0, 0), [])

    def test_events_page_falls_back_without_index(self) -> None:
        # If a segment's .idx is missing, events_page still returns the right
        # window by parsing the segment (the index is advisory).
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ledger.jsonl"
            ledger = self._rotated_ledger(path)
            Path(str(path) + ".1.idx").unlink()  # drop the first archive's index
            self.assertEqual(
                [e.payload["n"] for e in ledger.events_page(0, 6)], [1, 2, 3, 4, 5, 6]
            )

    def test_events_page_matches_full_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = self._rotated_ledger(Path(temp_dir) / "ledger.jsonl")
            paged = [e.entry_hash for e in ledger.events_page(0, 100)]
            full = [e.entry_hash for e in ledger.events(all_segments=True)]
            self.assertEqual(paged, full)
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
