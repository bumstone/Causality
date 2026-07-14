import errno
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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
    def test_file_lock_rejects_symlinked_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "state.jsonl"
            outside = root / "outside.lock"
            outside.write_bytes(b"")
            try:
                Path(str(path) + ".lock").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "lock sidecar must not be a symlink"):
                with file_lock(path):
                    pass

            self.assertEqual(outside.read_bytes(), b"")

    def test_file_lock_rejects_hardlinked_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside:
            path = Path(temp_dir) / "state.jsonl"
            source = Path(outside) / "outside.lock"
            source.write_bytes(b"")
            try:
                os.link(source, Path(str(path) + ".lock"))
            except OSError as exc:
                self.skipTest(f"hard links unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "lock sidecar must not be a hard link"):
                with file_lock(path):
                    pass

            self.assertEqual(source.read_bytes(), b"")

    def test_append_rejects_hardlinked_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside:
            path = Path(temp_dir) / "state.jsonl"
            source = Path(outside) / "outside.jsonl"
            source.write_bytes(b"")
            try:
                os.link(source, path)
            except OSError as exc:
                self.skipTest(f"hard links unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "append target must not be a hard link"):
                DurableJsonl(path).append('{"safe": false}')

            self.assertEqual(source.read_bytes(), b"")

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

    def test_first_append_fsyncs_parent_dir_then_skips(self) -> None:
        # codex r3409054886: the append that creates the file must fsync the
        # parent directory (so the new file's directory entry is durable);
        # appends to an already-existing file must not (keeps append O(1)).
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableJsonl(Path(tmp) / "log.jsonl")
            with mock.patch("causality.durable._fsync_dir") as fsync_dir:
                store.append(json.dumps({"n": 1}))
                self.assertEqual(fsync_dir.call_count, 1)
                store.append(json.dumps({"n": 2}))
                self.assertEqual(fsync_dir.call_count, 1)

    def test_file_lock_is_reentrant_across_sequential_acquires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            with file_lock(path):
                pass
            with file_lock(path):  # releasing then re-acquiring must not deadlock
                pass


class FsyncErrorPropagationTests(unittest.TestCase):
    # codex r3408027988: a data-file fsync failure means the write is NOT durable,
    # so it must propagate -- only an *unsupported directory* fsync is best-effort.
    def test_append_propagates_data_fsync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DurableJsonl(Path(tmp) / "log.jsonl")
            with mock.patch("causality.durable.os.fsync", side_effect=OSError(errno.EIO, "io")):
                with self.assertRaises(OSError):
                    store.append(json.dumps({"n": 1}))

    def test_write_text_durably_propagates_data_fsync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("causality.durable.os.fsync", side_effect=OSError(errno.ENOSPC, "nospc")):
                with self.assertRaises(OSError):
                    write_text_durably(Path(tmp) / "state.json", "data")

    def test_directory_fsync_unsupported_is_best_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            real_fsync = os.fsync
            calls = {"n": 0}

            def fake_fsync(fd: int) -> None:
                calls["n"] += 1
                # 1st fsync = the data file (must succeed); 2nd = the directory,
                # which we make report "unsupported".
                if calls["n"] >= 2:
                    raise OSError(errno.EINVAL, "dir fsync unsupported")
                real_fsync(fd)

            with mock.patch("causality.durable.os.fsync", side_effect=fake_fsync):
                write_text_durably(path, "data")  # must NOT raise
            self.assertEqual(path.read_text(encoding="utf-8"), "data")


if __name__ == "__main__":
    unittest.main()
