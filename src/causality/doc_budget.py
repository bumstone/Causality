"""Doc budget (ADR 0010): keep AI-generated working docs caveman-terse and small.

Long machine-written Markdown wastes tokens at generation and on every later
load. This module is the operational check: given a set of doc paths it reports
which exceed the per-file character budget, so generation can stay within it.

Exempt: human-canonical docs (READMEs, THIRD_PARTY_NOTICES, LICENSE) -- those
are curated, not AI working notes. Pre-rule docs are grandfathered (the check is
advisory; trim opportunistically).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

DEFAULT_DOC_MAX_CHARS = 2000
DEFAULT_EXEMPT = ("README*.md", "THIRD_PARTY_NOTICES.md", "LICENSE")


@dataclass(frozen=True)
class DocSize:
    path: str
    chars: int
    max_chars: int

    @property
    def over(self) -> bool:
        return self.chars > self.max_chars

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "chars": self.chars, "max_chars": self.max_chars, "over": self.over}


def _exempt(path: str, patterns: Sequence[str]) -> bool:
    name = Path(path).name
    return any(fnmatch(path, pat) or fnmatch(name, pat) for pat in patterns)


def expand_markdown(paths: Iterable[str | Path]) -> list[str]:
    """Expand any directory argument to its ``*.md`` children; pass files through.

    A directory handed straight to :func:`check_docs` would raise
    ``IsADirectoryError`` (an ``OSError``) and be silently skipped, giving a
    false "0 over" pass (codex review r3407301817). Expanding first avoids that.
    """
    out: list[str] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            out.extend(sorted(str(child) for child in path.rglob("*.md")))
        else:
            out.append(str(p))
    return out


def check_docs(
    paths: Iterable[str | Path],
    *,
    max_chars: int = DEFAULT_DOC_MAX_CHARS,
    exempt: Sequence[str] = DEFAULT_EXEMPT,
) -> list[DocSize]:
    """Measure each readable, non-exempt doc's character count against the budget."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    sizes: list[DocSize] = []
    for path in paths:
        sp = str(path)
        if _exempt(sp, exempt):
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Unreadable or non-UTF8 file: skip rather than crash the check.
            continue
        sizes.append(DocSize(path=sp, chars=len(text), max_chars=max_chars))
    return sizes


def over_budget(sizes: Iterable[DocSize]) -> list[DocSize]:
    return [d for d in sizes if d.over]


def format_report(sizes: Sequence[DocSize], *, max_chars: int = DEFAULT_DOC_MAX_CHARS) -> str:
    over = over_budget(sizes)
    head = f"Doc budget: {len(over)}/{len(sizes)} over {max_chars} chars"
    lines = [head]
    for d in sorted(sizes, key=lambda x: -x.chars):
        mark = "OVER " if d.over else "ok   "
        lines.append(f"  {mark}{d.chars:6} {d.path}")
    return "\n".join(lines)
