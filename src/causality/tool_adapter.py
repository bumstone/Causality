"""Bundled, gate-enforcing tool executors (ADR 0001 §2.3, ADR 0003).

A task's ``work`` callback previously had to hand-roll every side effect and
route each one through the :class:`ExecutionAdapter`; only the browser had a
ready adapter (:mod:`browser_adapter`). This bundles the other common ones --
run a subprocess, write a file, read a file -- so each call:

1. passes the contract's per-action gates (non_goals / allowed_tools / risk)
   before it touches the world (a refusal raises ``ActionBlocked`` and the run
   terminates with that decision), and
2. lands a ``TOOL_CALL`` / ``EVIDENCE`` record in the ledger afterwards, so reuse
   stays auditable.

Subprocesses run from a **list of args with no shell**, so a value can never be
interpolated into a shell command. The ledger keeps the raw argv as audit
evidence; the skill distiller redacts secrets when copying any of it into the
shared skill library (see :mod:`skills`).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .browser_adapter import CommandResult
from .contracts import AuditEventType, EvidenceKind, GateDecision
from .execution import ActionBlocked, ExecutionAdapter
from .gates import GateResult
from .ledger import EvidenceLedger


def _is_within(path: Path, base: Path) -> bool:
    """True if ``path`` is ``base`` or a descendant of it (both already resolved)."""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False

# Test seam: run a command (argv list) and return its result. The default runs a
# real subprocess; tests inject a fake so the suite never spawns a process.
CommandRunner = Callable[[Sequence[str]], CommandResult]
EffectHook = Callable[[], None]


def utf8_size(value: str | bytes | None) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    return len(value.encode("utf-8"))


@dataclass
class ToolAdapter:
    """Gate-enforcing executors handed to (or built by) a task's ``work``.

    Wraps the per-run :class:`ExecutionAdapter` (gating) and the ledger
    (auditing). Construct it inside ``work`` as ``ToolAdapter(runtime.ledger,
    adapter)`` and call :meth:`run` / :meth:`write_text` / :meth:`read_text`
    instead of touching the world directly.
    """

    ledger: EvidenceLedger
    execution: ExecutionAdapter
    runner: CommandRunner | None = None
    # Workspace root that relative file paths AND relative write_scope entries
    # resolve against, so a contract scope like ".causality/" matches
    # <root>/.causality regardless of the process cwd (codex r3448146018).
    root: Path = field(default_factory=Path.cwd)
    last_event_hash: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        # Resolve at construction so the root anchoring is independent of any
        # later cwd change (codex r3448157732).
        self.root = Path(self.root).resolve()

    def _resolved(self, path: str | Path) -> Path:
        """Absolute, symlink-resolved path; a relative one is anchored to root."""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return candidate.resolve()

    def run(
        self,
        args: Sequence[str],
        *,
        tool: str = "shell",
        action_kind: str = "tool_call",
        description: str | None = None,
        timeout: float = 30.0,
        cwd: str | Path | None = None,
        before_effect: EffectHook | None = None,
    ) -> CommandResult:
        """Run ``args`` through the gates and conservatively mark it as mutating."""
        return self._run(
            args,
            tool=tool,
            action_kind=action_kind,
            description=description,
            timeout=timeout,
            cwd=cwd,
            mutates_task=True,
            environment_overrides={},
            before_effect=before_effect,
        )

    def _run(
        self,
        args: Sequence[str],
        *,
        tool: str,
        action_kind: str,
        description: str | None,
        timeout: float,
        cwd: str | Path | None,
        mutates_task: bool,
        environment_overrides: Mapping[str, str],
        before_effect: EffectHook | None = None,
    ) -> CommandResult:
        """Internal runner; verification owns the only read-only command path."""
        argv = [str(arg) for arg in args]
        if not argv:
            raise ValueError("run() requires a non-empty command")
        desc = description if description is not None else " ".join(argv)
        # Subprocesses run from the adapter root (or an explicit cwd resolved
        # against it), so a relative command operates on the same tree as file
        # ops and write_scope, not the ambient process cwd (codex r3448164499).
        workdir = self._resolved(cwd) if cwd is not None else self.root

        def _do() -> CommandResult:
            if before_effect is not None:
                before_effect()
            if self.runner is not None:
                return self.runner(argv)
            completed = subprocess.run(  # noqa: S603 - argv list, never shell=True
                argv,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
                cwd=str(workdir),
                env={**os.environ, **environment_overrides},
            )
            return CommandResult(completed.returncode, completed.stdout, completed.stderr)

        with self.execution.runtime.execution_lock():
            result = self.execution.execute(
                tool=tool, action_kind=action_kind, description=desc, run=_do
            )
            self._record(
                AuditEventType.TOOL_CALL,
                {
                    "tool": tool,
                    "argv": argv,
                    "exit_code": result.exit_code,
                    "stdout_bytes": utf8_size(result.stdout),
                    "stderr_bytes": utf8_size(result.stderr),
                    "mutates_task": mutates_task,
                    "environment_overrides": dict(environment_overrides),
                },
            )
        return result

    def write_text(
        self,
        path: str | Path,
        content: str,
        *,
        tool: str = "file.write",
        action_kind: str = "write",
        encoding: str = "utf-8",
        before_effect: EffectHook | None = None,
    ) -> Path:
        """Write ``content`` to ``path`` through the gates; record the artifact."""
        target = self._resolved(path)
        # Honor the contract's frozen file boundary BEFORE touching the disk: a
        # write outside a declared write_scope is a STOP, like a non-goal breach
        # (codex r3448136006). The generic gates do not cover write_scope.
        self._enforce_write_scope(target)

        def _do() -> Path:
            if before_effect is not None:
                before_effect()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding=encoding)
            return target

        with self.execution.runtime.execution_lock():
            self.execution.execute(
                tool=tool, action_kind=action_kind, description=f"write file {target}", run=_do
            )
            event = self.execution.runtime.record_evidence(  # type: ignore[attr-defined]
                self.execution.contract,
                EvidenceKind.ARTIFACT_HASH,
                {
                    "tool": tool,
                    "path": str(target),
                    "bytes": len(content.encode(encoding)),
                    "mutates_task": True,
                },
                artifact_paths=[target],
            )
            self.last_event_hash = event.entry_hash
        return target

    def read_text(
        self,
        path: str | Path,
        *,
        tool: str = "file.read",
        action_kind: str = "tool_call",
        encoding: str = "utf-8",
        before_effect: EffectHook | None = None,
    ) -> str:
        """Read ``path`` through the gates; record a TOOL_CALL."""
        target = self._resolved(path)

        def _do() -> str:
            if before_effect is not None:
                before_effect()
            return target.read_text(encoding=encoding)

        content = self.execution.execute(
            tool=tool, action_kind=action_kind, description=f"read file {target}", run=_do
        )
        self._record(
            AuditEventType.TOOL_CALL,
            {
                "tool": tool,
                "path": str(target),
                "bytes": len(content.encode(encoding)),
                "mutates_task": False,
            },
        )
        return content

    def _enforce_write_scope(self, target: Path) -> None:
        """Block a write whose resolved path is outside the contract's write_scope.

        An empty ``write_scope`` declares no file restriction (any path passes).
        When a scope IS declared, a path outside every scoped directory records a
        STOP gate decision for audit and raises :class:`ActionBlocked`, so the run
        terminates with that decision instead of writing out of bounds.
        """
        scope = self.execution.contract.permissions.write_scope
        if not scope:
            return
        # ``target`` is already resolved; resolve relative scope entries against
        # the same workspace root so the comparison is consistent (r3448146018).
        if any(_is_within(target, self._resolved(entry)) for entry in scope):
            return
        result = GateResult(
            GateDecision.STOP,
            (f"path is outside the contract's write_scope: {target}",),
        )
        self._record(AuditEventType.GATE_DECISION, result.to_dict())
        raise ActionBlocked(
            result, tool="file.write", action_kind="write", description=f"write file {target}"
        )

    def _record(
        self,
        event_type: AuditEventType,
        payload: dict,
    ) -> None:
        event = self.ledger.append(
            event_type,
            payload,
            contract_id=self.execution.contract.goal_id,
        )
        self.last_event_hash = event.entry_hash
