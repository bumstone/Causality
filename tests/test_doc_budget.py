from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.doc_budget import check_docs, expand_markdown, format_report, over_budget


class DocBudgetTests(unittest.TestCase):
    def _write(self, root: Path, name: str, chars: int) -> Path:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x" * chars, encoding="utf-8")
        return p

    def test_flags_only_over_budget(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            small = self._write(root, "small.md", 100)
            big = self._write(root, "big.md", 2500)

            sizes = check_docs([small, big], max_chars=2000)
            over = over_budget(sizes)

            self.assertEqual(len(sizes), 2)
            self.assertEqual([s.path for s in over], [str(big)])

    def test_exempt_readme_and_license(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            readme = self._write(root, "README.md", 9000)
            ko = self._write(root, "README.ko.md", 9000)
            third = self._write(root, "THIRD_PARTY_NOTICES.md", 9000)
            adr = self._write(root, "docs/adr/0001.md", 9000)

            sizes = check_docs([readme, ko, third, adr], max_chars=2000)

            # Only the ADR is measured; canonical human docs are exempt.
            self.assertEqual([s.path for s in sizes], [str(adr)])

    def test_missing_file_skipped(self) -> None:
        sizes = check_docs([Path("/no/such/file.md")], max_chars=2000)
        self.assertEqual(sizes, [])

    def test_zero_budget_rejected(self) -> None:
        with self.assertRaises(ValueError):
            check_docs([], max_chars=0)

    def test_format_report_marks_over(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            big = self._write(Path(d), "big.md", 2500)
            report = format_report(check_docs([big], max_chars=2000), max_chars=2000)
            self.assertIn("OVER", report)
            self.assertIn("1/1 over 2000", report)

    def test_adr_0010_is_within_budget(self) -> None:
        # Dogfood: the rule's own ADR must obey the rule. Split assertions so an
        # empty result fails clearly instead of IndexError-ing in the message
        # (copilot review r3407679320).
        adr = Path(__file__).resolve().parents[1] / "docs/adr/0010-caveman-doc-budget.md"
        sizes = check_docs([adr], max_chars=2000)
        self.assertEqual(len(sizes), 1)
        self.assertFalse(sizes[0].over, f"ADR 0010 is {sizes[0].chars} chars (>2000)")

    def test_expand_markdown_expands_dirs_and_passes_files(self) -> None:
        # codex review r3407301817: a directory arg must expand to its *.md
        # children, not be silently skipped by check_docs.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._write(root, "docs/a.md", 100)
            self._write(root, "docs/sub/b.md", 100)
            self._write(root, "docs/notes.txt", 100)

            expanded = expand_markdown([root / "docs", str(root / "loose.md")])

            self.assertEqual(
                sorted(Path(p).name for p in expanded), ["a.md", "b.md", "loose.md"]
            )
            # The expanded dir's over-budget child is now measured.
            self._write(root, "docs/big.md", 2500)
            over = over_budget(check_docs(expand_markdown([root / "docs"]), max_chars=2000))
            self.assertIn("big.md", [Path(o.path).name for o in over])

    def test_non_utf8_file_is_skipped(self) -> None:
        # copilot review r3407679325: a non-UTF8 file must be skipped, not crash.
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.md"
            bad.write_bytes(b"\xff\xfe not utf-8 \x80")
            self.assertEqual(check_docs([bad], max_chars=2000), [])


if __name__ == "__main__":
    unittest.main()
