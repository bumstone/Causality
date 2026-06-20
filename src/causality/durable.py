"""Centralized, durable file I/O for the append-only / state stores (ADR 0011 §2.2).

``EvidenceLedger``, ``TypedMemory``, ``SkillStore``, and ``Agenda`` route every
file move through here so durability lives in one place instead of four:

- **R4a** extracted the moves (append a JSON line, read lines, rewrite all,
  replace a JSON state doc) with byte-identical output.
- **R4b** makes them crash-safe: :func:`write_text_durably` writes a temp
  sibling, ``fsync``s it, then ``os.replace``s it into place (atomic) and
  ``fsync``s the directory; :meth:`DurableJsonl.append` ``fsync``s each record
  (and the parent directory on the append that creates the file) and truncates a
  torn trailing line (a half-written record from a crashed append) before
  writing, so records never merge; :meth:`DurableJsonl.read_lines`
  drops a torn trailing line on read.
- **R4c** serializes writers: :func:`file_lock` takes an exclusive ``flock`` on a
  ``<path>.lock`` sidecar. ``EvidenceLedger.append`` holds it across its
  read-latest-hash + append so the hash chain cannot fork across processes.
"""

from __future__ import annotations

import errno
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

try:  # POSIX only; elsewhere the lock degrades to a best-effort no-op.
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]


# Directory fsync is unsupported on some platforms/filesystems (it raises one of
# these). Only those cases are best-effort: a *data*-file fsync failure is never
# swallowed, because it means the write is NOT durable and the caller -- which
# now believes the record is persisted -- must hear about it (codex r3408027988).
_DIR_FSYNC_UNSUPPORTED = {
    getattr(errno, name)
    for name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP")
    if hasattr(errno, name)
}


def _fsync_dir(directory: Path) -> None:
    # Persist the directory entry so a rename (os.replace) survives a crash.
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:  # platform cannot open a directory fd (e.g. Windows)
        return
    try:
        os.fsync(dir_fd)
    except OSError as exc:  # unsupported here -> best-effort; real I/O errors raise
        if exc.errno not in _DIR_FSYNC_UNSUPPORTED:
            raise
    finally:
        os.close(dir_fd)


@contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    """Exclusive lock keyed on a ``<path>.lock`` sidecar, held for one write.

    Two writers to the same store serialize instead of interleaving (across
    processes and threads). On platforms without ``fcntl`` this is a best-effort
    no-op (ADR 0011 §4: cross-process safety is POSIX-only).
    """
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:  # pragma: no cover - non-POSIX
        yield
        return
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def write_text_durably(path: str | Path, text: str, *, lock: bool = True) -> None:
    """Atomically replace the whole file at ``path`` with ``text``.

    Writes a temp sibling, ``fsync``s it, ``os.replace``s it into place, then
    ``fsync``s the directory, so a crash mid-write leaves either the old file or
    the complete new one -- never a truncated mix. Pass ``lock=False`` when the
    caller already holds :func:`file_lock` for ``path``.
    """
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    def _do() -> None:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(file_path.parent), prefix=f".{file_path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, file_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
        _fsync_dir(file_path.parent)

    if lock:
        with file_lock(file_path):
            _do()
    else:
        _do()


class DurableJsonl:
    """A line-delimited JSON file: append a record, read records, rewrite all.

    Callers serialize their own dict shape and pass the resulting line, so output
    bytes are unchanged from the prior hand-rolled writes. Blank lines are skipped
    on read; a torn trailing line is dropped (read) and truncated (next append).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, line: str, *, lock: bool = True) -> None:
        """Append one record line, ``fsync``ed. Pass ``lock=False`` if held."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def _do() -> None:
            self._repair_torn_tail()
            created = not self.path.exists()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if created:
                # The append that creates the file must persist the parent
                # directory entry too, else a crash after this returns can lose
                # the whole new file and its first record (codex r3409054886).
                # Later appends don't change the directory, so they skip this and
                # stay amortized O(1).
                _fsync_dir(self.path.parent)

        if lock:
            with file_lock(self.path):
                _do()
        else:
            _do()

    def read_lines(self) -> list[str]:
        """Return the non-blank record lines, in order; a torn tail is dropped."""
        return self.read_lines_with_torn()[0]

    def read_lines_with_torn(self) -> tuple[list[str], bool]:
        """Like :meth:`read_lines`, plus whether the tail was torn.

        The torn partial of a crashed append is dropped from the returned lines,
        but its presence is reported so a size-guarded cache does not key its
        freshness to a ``stat`` size that still counts the dropped bytes -- a
        later repair+append of the same length would otherwise leave the size
        unchanged and hide the new record (codex r3445819560).
        """
        if not self.path.exists():
            return [], False
        raw = self.path.read_text(encoding="utf-8")
        if not raw:
            return [], False
        # Every complete record ends in "\n", so split() leaves a trailing piece:
        # "" when the tail is clean, or the torn partial bytes of a crashed append.
        # Either way the last piece is never a complete record -- drop it, and
        # flag the torn case so size-based caches can refuse to trust the size.
        torn = not raw.endswith("\n")
        parts = raw.split("\n")[:-1]
        return [part for part in parts if part.strip()], torn

    def rewrite(self, lines: Iterable[str], *, lock: bool = True) -> None:
        materialized = list(lines)
        text = ("\n".join(materialized) + "\n") if materialized else ""
        write_text_durably(self.path, text, lock=lock)

    def _ends_with_newline(self) -> bool:
        with self.path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                return True  # empty file: nothing torn
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) == b"\n"

    def _repair_torn_tail(self) -> None:
        # O(1) common case: a newline-terminated tail needs no repair, so append
        # stays amortized O(1). Only a torn tail (crashed prior append) pays the
        # rare full rewrite that drops the partial bytes.
        if not self.path.exists() or self._ends_with_newline():
            return
        data = self.path.read_bytes()
        cut = data.rfind(b"\n")
        repaired = data[: cut + 1] if cut != -1 else b""
        write_text_durably(self.path, repaired.decode("utf-8"), lock=False)
