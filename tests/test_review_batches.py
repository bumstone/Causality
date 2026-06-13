from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.review_batches import (
    FileChange,
    parse_numstat,
    plan_review_batches,
    total_lines,
)


class ParseNumstatTests(unittest.TestCase):
    def test_parses_added_deleted_and_path(self) -> None:
        text = "10\t5\tsrc/a.py\n0\t3\tsrc/b.py\n"
        changes = parse_numstat(text)
        self.assertEqual([c.path for c in changes], ["src/a.py", "src/b.py"])
        self.assertEqual(changes[0].lines, 15)
        self.assertEqual(changes[1].lines, 3)

    def test_binary_files_count_zero(self) -> None:
        changes = parse_numstat("-\t-\tdocs/assets/diagram.png\n")
        self.assertEqual(changes[0].lines, 0)

    def test_ignores_blank_and_malformed_lines(self) -> None:
        self.assertEqual(parse_numstat("\n  \nnot-a-row\n"), [])


class PlanReviewBatchesTests(unittest.TestCase):
    def test_packs_under_budget_into_one_batch(self) -> None:
        changes = [FileChange("a.py", 100, 0), FileChange("b.py", 200, 0)]
        batches = plan_review_batches(changes, max_lines=1000)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].lines, 300)
        self.assertFalse(batches[0].oversized)

    def test_splits_when_budget_exceeded(self) -> None:
        changes = [
            FileChange("a.py", 600, 0),
            FileChange("b.py", 600, 0),
            FileChange("c.py", 100, 0),
        ]
        batches = plan_review_batches(changes, max_lines=1000)
        # a(600) -> batch1; b(600) would overflow -> batch2 with c(100).
        self.assertEqual(len(batches), 2)
        self.assertTrue(all(b.lines <= 1000 for b in batches))
        self.assertEqual(sum(len(b.files) for b in batches), 3)
        # indices are sequential starting at 0
        self.assertEqual([b.index for b in batches], [0, 1])

    def test_single_oversized_file_is_flagged_in_own_batch(self) -> None:
        changes = [FileChange("small.py", 50, 0), FileChange("huge.py", 2500, 0)]
        batches = plan_review_batches(changes, max_lines=1000)
        oversized = [b for b in batches if b.oversized]
        self.assertEqual(len(oversized), 1)
        self.assertEqual(oversized[0].files[0].path, "huge.py")
        self.assertEqual(oversized[0].lines, 2500)

    def test_exclude_globs_and_zero_line_changes_dropped(self) -> None:
        changes = [
            FileChange("src/a.py", 100, 0),
            FileChange("docs/assets/x.svg", 400, 0),
            FileChange("renamed.py", 0, 0),
        ]
        batches = plan_review_batches(changes, max_lines=1000, exclude=["docs/assets/*"])
        paths = [f.path for b in batches for f in b.files]
        self.assertEqual(paths, ["src/a.py"])  # svg excluded, zero-line dropped

    def test_total_lines(self) -> None:
        self.assertEqual(total_lines([FileChange("a", 10, 5), FileChange("b", 0, 3)]), 18)

    def test_zero_max_lines_rejected(self) -> None:
        with self.assertRaises(ValueError):
            plan_review_batches([FileChange("a.py", 1, 0)], max_lines=0)


if __name__ == "__main__":
    unittest.main()
