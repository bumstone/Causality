"""Centralized file I/O for the append-only / state stores (ADR 0011 §2.2, R4).

``EvidenceLedger``, ``TypedMemory``, ``SkillStore``, and ``Agenda`` each
hand-rolled the same handful of file moves: append a JSON line, read back the
non-blank lines, rewrite the whole file, or replace a JSON state document.
**R4a** pulls those moves into this one module so the durability work can land
once here instead of in four places:

- **R4b** will make :func:`write_text_durably` write to a temp sibling, ``fsync``
  it, then ``os.replace`` (so a crash mid-write cannot truncate the live file),
  and make :meth:`DurableJsonl.append` ``fsync`` and :meth:`DurableJsonl.read_lines`
  tolerate a torn final line.
- **R4c** will wrap the writes in an ``flock`` to serialize cross-process writers.

This step (R4a) is a **pure refactor**: callers still own serialization, so the
output is byte-for-byte identical and read semantics (skip blank lines) are
unchanged. No ``fsync``/lock/atomic-rename is added yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


def write_text_durably(path: str | Path, text: str) -> None:
    """Replace the whole file at ``path`` with ``text`` (parent dir ensured).

    R4a behavior is a plain write, identical to the previous ``write_text``
    calls. R4b will route this through a temp file + ``fsync`` + ``os.replace``
    so the replacement is atomic and survives a crash.
    """
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(text, encoding="utf-8")


class DurableJsonl:
    """A line-delimited JSON file: append a record line, read lines, rewrite all.

    Callers serialize their own dict shape and pass the resulting line, so this
    helper is serialization-agnostic and preserves each store's exact byte
    output. Blank lines are skipped on read, matching the prior hand-rolled
    loops.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, line: str) -> None:
        """Append one record line (a trailing newline is added)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read_lines(self) -> list[str]:
        """Return the non-blank lines, in order. Missing file -> empty list."""
        if not self.path.exists():
            return []
        lines: list[str] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    lines.append(line)
        return lines

    def rewrite(self, lines: Iterable[str]) -> None:
        """Replace the file with ``lines`` (newline-joined, trailing newline).

        An empty iterable writes an empty file, matching the prior behavior.
        Routed through :func:`write_text_durably` so R4b's atomic rewrite covers
        it for free.
        """
        materialized = list(lines)
        text = ("\n".join(materialized) + "\n") if materialized else ""
        write_text_durably(self.path, text)
