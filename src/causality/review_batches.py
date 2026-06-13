"""Reviewable-change batching (ADR 0009).

Keep every review unit within a bounded line budget so a reviewer -- a human or
an external review agent -- never has to read more than ~1000 changed lines at
once. This module is the operational core of the rule: it partitions a set of
file changes (from ``git diff --numstat``) into ordered review batches, each
within ``max_lines``, and flags any single file that alone exceeds the budget
(which must then be split internally by hunk/line-range).

Two paths use it (ADR 0009):

- PR path: before opening PRs, plan how to split work so no PR exceeds the
  budget.
- Non-PR path: partition an un-reviewed working/branch diff into batches and run
  the review (e.g. ``/code-review``) once per batch.

The line count is ``added + deleted`` (what a reviewer actually reads), matching
how forges report "lines changed". Binary files contribute 0 reviewable lines.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any

DEFAULT_MAX_LINES = 1000


@dataclass(frozen=True)
class FileChange:
    path: str
    added: int
    deleted: int

    @property
    def lines(self) -> int:
        return self.added + self.deleted

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "added": self.added, "deleted": self.deleted, "lines": self.lines}


@dataclass(frozen=True)
class ReviewBatch:
    index: int
    files: tuple[FileChange, ...]
    lines: int
    oversized: bool  # a single file exceeds max_lines -> split internally by hunk

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "lines": self.lines,
            "oversized": self.oversized,
            "files": [f.to_dict() for f in self.files],
        }


def parse_numstat(text: str) -> list[FileChange]:
    """Parse ``git diff --numstat`` output into :class:`FileChange` rows.

    Each line is ``added<TAB>deleted<TAB>path``. Binary files show ``-`` for the
    counts and are recorded as 0 reviewable lines. Rename lines that git emits
    as ``old => new`` are kept verbatim as the path.
    """
    changes: list[FileChange] = []
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_s, deleted_s, path = parts[0], parts[1], "\t".join(parts[2:]).strip()
        added = 0 if added_s.strip() == "-" else int(added_s)
        deleted = 0 if deleted_s.strip() == "-" else int(deleted_s)
        changes.append(FileChange(path=path, added=added, deleted=deleted))
    return changes


def _excluded(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch(path, pat) for pat in patterns)


def plan_review_batches(
    changes: Iterable[FileChange],
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    exclude: Sequence[str] = (),
) -> list[ReviewBatch]:
    """Partition ``changes`` into ordered batches no larger than ``max_lines``.

    Files are grouped by path (so same-directory changes stay together) and
    greedily packed. A single file larger than ``max_lines`` becomes its own
    batch flagged ``oversized`` -- the rule then requires splitting it by hunk.
    Excluded paths (fnmatch globs, e.g. generated artifacts) and zero-line
    changes are dropped.
    """
    if max_lines <= 0:
        raise ValueError("max_lines must be positive")

    kept = sorted(
        (c for c in changes if c.lines > 0 and not _excluded(c.path, exclude)),
        key=lambda c: c.path,
    )

    batches: list[ReviewBatch] = []
    current: list[FileChange] = []
    current_lines = 0

    def flush() -> None:
        nonlocal current, current_lines
        if current:
            batches.append(
                ReviewBatch(index=len(batches), files=tuple(current), lines=current_lines, oversized=False)
            )
            current = []
            current_lines = 0

    for change in kept:
        if change.lines > max_lines:
            flush()
            batches.append(
                ReviewBatch(index=len(batches), files=(change,), lines=change.lines, oversized=True)
            )
            continue
        if current and current_lines + change.lines > max_lines:
            flush()
        current.append(change)
        current_lines += change.lines

    flush()
    return batches


def total_lines(changes: Iterable[FileChange]) -> int:
    return sum(c.lines for c in changes)


def format_plan(batches: Sequence[ReviewBatch], *, max_lines: int = DEFAULT_MAX_LINES) -> str:
    """Render a human-readable review plan."""
    grand = sum(b.lines for b in batches)
    out = [f"Review plan: {len(batches)} batch(es), {grand} reviewable lines, budget {max_lines}/batch"]
    for batch in batches:
        flag = "  [OVERSIZED -- split by hunk]" if batch.oversized else ""
        out.append(f"  Batch {batch.index + 1}: {batch.lines} lines, {len(batch.files)} file(s){flag}")
        for f in batch.files:
            out.append(f"    {f.path} (+{f.added}/-{f.deleted} = {f.lines})")
    if not batches:
        out.append("  (no reviewable changes)")
    return "\n".join(out)
