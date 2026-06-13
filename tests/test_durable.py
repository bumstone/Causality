import json
import tempfile
import threading
import unittest
from pathlib import Path

from causality.durable import DurableJsonl, file_lock, write_text_durably


class WriteTextDurablyTests(unittest.TestCase):
    def test_writes_exact_text_and_creates_parents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "dir" / "state.json"
            write_text_durably(path, '{"a": 1}\n')
            self.assertEqual(path.read_text(encoding="utf-8"), '{"a": 1}\n')

    def test_replaces_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            write_text_durably(path, "first")
            write_text_durably(path, "second")
            self.assertEqual(path.read_text(encoding="utf-8"), "second")


class DurableJsonlTests(unittest.TestCase):
    def test_read_missing_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableJsonl(Path(tmp) / "missing.jsonl")
            self.assertEqual(store.read_lines(), [])

    def test_append_then_read_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableJsonl(Path(tmp) / "log.jsonl")
            store.append(json.dumps({"n": 1}))
            store.append(json.dumps({"n": 2}))
            parsed = [json.loads(line) for line in store.read_lines()]
            self.assertEqual(parsed, [{"n": 1}, {"n": 2}])

    def test_append_creates_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableJsonl(Path(tmp) / "a" / "b" / "log.jsonl")
            store.append(json.dumps({"ok": True}))
            self.assertEqual([json.loads(x) for x in store.read_lines()], [{"ok": True}])

    def test_read_skips_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            path.write_text('{"n": 1}\n\n   \n{"n": 2}\n', encoding="utf-8")
            store = DurableJsonl(path)
            self.assertEqual(len(store.read_lines()), 2)

    def test_rewrite_replaces_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableJsonl(Path(tmp) / "log.jsonl")
            store.append(json.dumps({"n": 1}))
            store.rewrite([json.dumps({"n": 9}), json.dumps({"n": 10})])
            parsed = [json.loads(line) for line in store.read_lines()]
            self.assertEqual(parsed, [{"n": 9}, {"n": 10}])

    def test_rewrite_empty_writes_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            store = DurableJsonl(path)
            store.append(json.dumps({"n": 1}))
            store.rewrite([])
            self.assertEqual(path.read_text(encoding="utf-8"), "")
            self.assertEqual(store.read_lines(), [])

    def test_rewrite_trailing_newline_matches_prior_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            DurableJsonl(path).rewrite(["a", "b"])
            self.assertEqual(path.read_text(encoding="utf-8"), "a\nb\n")


class DurabilityTests(unittest.TestCase):
    def test_atomic_replace_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            write_text_durably(d / "state.json", '{"ok": 1}')
            leftovers = [p.name for p in d.iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])
            self.assertEqual((d / "state.json").read_text(encoding="utf-8"), '{"ok": 1}')

    def test_read_drops_torn_trailing_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            # One complete record then a torn (no trailing newline) partial write.
            path.write_text('{"n": 1}\n{"n": 2', encoding="utf-8")
            parsed = [json.loads(x) for x in DurableJsonl(path).read_lines()]
            self.assertEqual(parsed, [{"n": 1}])

    def test_append_repairs_torn_tail_so_records_never_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            path.write_text('{"n": 1}\n{"n": 2', encoding="utf-8")  # torn tail
            store = DurableJsonl(path)
            store.append(json.dumps({"n": 3}))
            parsed = [json.loads(x) for x in store.read_lines()]
            # Torn partial dropped; the new record lands on its own clean line.
            self.assertEqual(parsed, [{"n": 1}, {"n": 3}])

    def test_lock_serializes_concurrent_appends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"

            def worker(base: int) -> None:
                store = DurableJsonl(path)
                for i in range(base, base + 50):
                    store.append(json.dumps({"i": i}))

            threads = [threading.Thread(target=worker, args=(b,)) for b in (0, 100, 200)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            values = sorted(json.loads(x)["i"] for x in DurableJsonl(path).read_lines())
            # All 150 records present, each a valid JSON object (no interleaving).
            self.assertEqual(len(values), 150)
            self.assertEqual(len(set(values)), 150)

    def test_file_lock_is_reentrant_across_sequential_acquires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            with file_lock(path):
                pass
            with file_lock(path):  # releasing then re-acquiring must not deadlock
                pass


if __name__ == "__main__":
    unittest.main()
