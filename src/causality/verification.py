"""Executable verification bound to a frozen GoalContract snapshot."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from .contracts import (
    AuditEventType,
    EvidenceKind,
    GoalContract,
    StateTransition,
    VerificationRequirement,
    VerificationResult,
    contract_binding_payload,
    utc_now,
)
from .execution import ActionBlocked, ExecutionAdapter
from .ledger import sha256_file, sha256_text
from .tool_adapter import ToolAdapter, captured_output

if TYPE_CHECKING:
    from .orchestrator import Causality


_VOLATILE_DIRS = {
    ".causality",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


def _tree_digest(root: Path, visited: frozenset[Path]) -> str:
    """Digest a symlinked directory target without following nested links blindly."""
    resolved_root = root.resolve()
    if resolved_root in visited:
        return sha256_text(f"cycle:{resolved_root}")
    seen = visited | {resolved_root}
    rows: list[str] = []
    for directory, dirnames, filenames in os.walk(resolved_root, followlinks=False):
        base = Path(directory)
        dirnames.sort()
        filenames.sort()
        for name in tuple(dirnames):
            path = base / name
            if name in _VOLATILE_DIRS:
                dirnames.remove(name)
                continue
            relative = path.relative_to(resolved_root).as_posix()
            if path.is_symlink():
                rows.append(f"{relative}:{_path_signature(path, seen)}")
                dirnames.remove(name)
            else:
                try:
                    rows.append(
                        f"{relative}/:{stat.S_IMODE(path.lstat().st_mode):o}:directory"
                    )
                except OSError as exc:
                    rows.append(f"{relative}/:unreadable:{type(exc).__name__}")
        for name in filenames:
            path = base / name
            relative = path.relative_to(resolved_root).as_posix()
            rows.append(f"{relative}:{_path_signature(path, seen)}")
    return sha256_text("\n".join(rows))


def _path_signature(path: Path, visited: frozenset[Path] = frozenset()) -> str:
    try:
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            link_target = os.readlink(path)
            try:
                resolved = path.resolve(strict=True)
                if resolved.is_file():
                    target = (
                        f"file:{stat.S_IMODE(resolved.stat().st_mode):o}:"
                        f"{sha256_file(resolved)}"
                    )
                elif resolved.is_dir():
                    target = f"directory:{_tree_digest(resolved, visited)}"
                else:
                    target = "other"
            except OSError as exc:
                target = f"unreadable:{type(exc).__name__}"
            return f"{mode:o}:symlink:{link_target}:target:{target}"
        if stat.S_ISREG(metadata.st_mode):
            return f"{mode:o}:file:{sha256_file(path)}"
        if stat.S_ISDIR(metadata.st_mode):
            return f"{mode:o}:directory"
        return f"{mode:o}:other:{stat.S_IFMT(metadata.st_mode):o}"
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"


def _git_directories(root: Path) -> tuple[Path, Path] | None:
    marker = root / ".git"
    try:
        if marker.is_dir():
            git_dir = marker.resolve()
        elif marker.is_file():
            first_line = marker.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            if not first_line.lower().startswith("gitdir:"):
                return None
            value = first_line.split(":", 1)[1].strip()
            candidate = Path(value)
            git_dir = (marker.parent / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        else:
            return None
    except (OSError, IndexError):
        return None

    common_dir = git_dir
    commondir = git_dir / "commondir"
    try:
        if commondir.is_file():
            value = commondir.read_text(encoding="utf-8", errors="replace").strip()
            candidate = Path(value)
            common_dir = (
                (git_dir / candidate).resolve()
                if not candidate.is_absolute()
                else candidate.resolve()
            )
    except OSError:
        pass
    return git_dir, common_dir


def _git_control_files(directory: Path) -> list[Path]:
    try:
        return [
            path
            for path in directory.rglob("*")
            if path.is_file() or path.is_symlink()
        ]
    except OSError:
        return []


def _is_ledger_runtime_path(path: Path, ledger: Path) -> bool:
    if path.parent != ledger.parent:
        return False
    name = path.name
    base = ledger.name
    if name in {
        base,
        f"{base}.lock",
        f"{base}.head",
        f"{base}.head.lock",
        f"{base}.idx",
        f"{base}.idx.lock",
    }:
        return True
    if name.startswith(f".{base}.") and name.endswith(".tmp"):
        return True
    if not name.startswith(f"{base}."):
        return False
    parts = name[len(base) + 1 :].split(".")
    return bool(parts[0].isdigit() and parts[1:] in ([], ["idx"], ["idx", "lock"]))


def workspace_fingerprint(
    root: Path,
    ledger_path: str | Path,
    artifact_paths: Iterable[str] = (),
) -> dict[str, str]:
    """Hash task files, excluding runtime/cache state and declared artifacts."""
    root = root.resolve()
    allowed_artifacts: set[Path] = set()
    for declared in artifact_paths:
        candidate = Path(declared)
        if not candidate.is_absolute():
            candidate = root / candidate
        allowed_artifacts.add(candidate.absolute())
    ledger = Path(ledger_path).resolve()
    fingerprint: dict[str, str] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in tuple(dirnames):
            path = base / name
            if name in _VOLATILE_DIRS:
                dirnames.remove(name)
                continue
            relative = path.relative_to(root).as_posix()
            fingerprint[f"{relative}/" if not path.is_symlink() else relative] = _path_signature(path)
            if path.is_symlink():
                dirnames.remove(name)
        for name in filenames:
            path = base / name
            if path.absolute() in allowed_artifacts:
                continue
            resolved = path.resolve()
            if _is_ledger_runtime_path(resolved, ledger):
                continue
            relative = path.relative_to(root).as_posix()
            fingerprint[relative] = _path_signature(path)

    git_directories = _git_directories(root)
    if git_directories is not None:
        git_dir, common_dir = git_directories
        git_label = ".git" if (root / ".git").is_dir() else ".gitdir"
        roots = [(git_label, git_dir)]
        if common_dir != git_dir:
            roots.append((".git-common", common_dir))
        for label, directory in roots:
            fingerprint[f"{label}/@identity"] = sha256_text(str(directory))
            for path in _git_control_files(directory):
                try:
                    relative = path.relative_to(directory).as_posix()
                except ValueError:
                    continue
                fingerprint[f"{label}/{relative}"] = _path_signature(path)
    return fingerprint


def workspace_fingerprint_digest(fingerprint: dict[str, str]) -> str:
    payload = json.dumps(fingerprint, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256_text(payload)


def workspace_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )


def snapshot_contract(
    runtime: "Causality",
    contract: GoalContract,
) -> GoalContract:
    """Return the durable binding and reject live-object widening/narrowing.

    State transitions are mutable; every other contract clause is frozen. The
    ledger chain must also be intact before its snapshot can authorize a command.
    """
    if not runtime.ledger.verify_chain():
        raise RuntimeError("ledger hash chain verification failed")
    payload = runtime.ledger.contract_snapshot(contract.goal_id)
    if payload is None:
        raise ValueError(f"contract snapshot not found: {contract.goal_id}")
    snapshot = GoalContract.from_mapping(payload)
    if contract_binding_payload(contract) != contract_binding_payload(snapshot):
        raise ValueError("live contract differs from durable contract snapshot")
    if not snapshot.workspace_root:
        raise ValueError("durable contract has no workspace_root")
    if Path(snapshot.workspace_root).resolve() != runtime.project_root:
        raise ValueError("contract workspace_root differs from runtime project root")
    return snapshot


def find_requirement(
    runtime: "Causality",
    contract: GoalContract,
    requirement_id: str,
) -> VerificationRequirement:
    for requirement in snapshot_contract(runtime, contract).verification_requirements:
        if requirement.id == requirement_id:
            return requirement
    raise ValueError(f"unknown verification requirement: {requirement_id}")


def _resolve_artifacts(
    requirement: VerificationRequirement,
    root: Path,
) -> tuple[dict[str, str | None], list[dict[str, object]], list[Path], list[str]]:
    actual: dict[str, str | None] = {}
    records: list[dict[str, object]] = []
    ledger_paths: list[Path] = []
    problems: list[str] = []
    for declared_path, expected in requirement.artifact_paths.items():
        candidate = Path(declared_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        mode: int | None = None
        file_type = "missing"
        metadata: os.stat_result | None = None
        try:
            metadata = candidate.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                file_type = "symlink"
            elif stat.S_ISREG(metadata.st_mode):
                file_type = "file"
            elif stat.S_ISDIR(metadata.st_mode):
                file_type = "directory"
            else:
                file_type = "other"
        except FileNotFoundError:
            pass
        except OSError as exc:
            file_type = "unreadable"
            problems.append(f"artifact unreadable: {declared_path}: {exc}")

        record: dict[str, object] = {
            "path": declared_path,
            "resolved_path": str(resolved),
            "expected_sha256": expected,
            "actual_sha256": None,
            "file_type": file_type,
            "mode": mode,
        }
        if not resolved.is_relative_to(root):
            actual[declared_path] = None
            problems.append(f"artifact path escapes project root: {declared_path}")
            records.append(record)
            continue

        digest: str | None = None
        if file_type == "file":
            try:
                digest = sha256_file(resolved)
            except OSError as exc:
                problems.append(f"artifact unreadable: {declared_path}: {exc}")
        elif file_type not in {"missing", "unreadable"}:
            problems.append(
                f"artifact must be a regular file, got {file_type}: {declared_path}"
            )
        actual[declared_path] = digest
        record["actual_sha256"] = digest
        if file_type in {"file", "missing"}:
            ledger_paths.append(resolved)
        records.append(record)
        if digest is None and file_type == "missing":
            problems.append(f"artifact missing: {declared_path}")
        elif expected is not None and digest != expected:
            problems.append(f"artifact hash mismatch: {declared_path}")
    return actual, records, ledger_paths, problems


def _artifact_state(records: list[dict[str, object]]) -> dict[str, tuple[object, ...]]:
    return {
        str(record["path"]): (
            record.get("resolved_path"),
            record.get("file_type"),
            record.get("mode"),
            record.get("actual_sha256"),
        )
        for record in records
    }


def execute_requirement(
    runtime: "Causality",
    contract: GoalContract,
    requirement_id: str,
    *,
    root: str | Path | None = None,
    before_effect: Callable[[], None] | None = None,
    transition_on_failure: bool = True,
) -> VerificationResult:
    with runtime.execution_lock():
        return _execute_requirement_locked(
            runtime,
            contract,
            requirement_id,
            root=root,
            before_effect=before_effect,
            transition_on_failure=transition_on_failure,
        )


def _execute_requirement_locked(
    runtime: "Causality",
    contract: GoalContract,
    requirement_id: str,
    *,
    root: str | Path | None = None,
    before_effect: Callable[[], None] | None = None,
    transition_on_failure: bool = True,
) -> VerificationResult:
    """Execute one frozen argv requirement through the normal action gates."""
    execution = ExecutionAdapter(runtime, contract)
    frozen = execution.contract
    requirement = next(
        (item for item in frozen.verification_requirements if item.id == requirement_id),
        None,
    )
    if requirement is None:
        raise ValueError(f"unknown verification requirement: {requirement_id}")
    if requirement.manual:
        raise ValueError(
            f"manual verification requirement '{requirement.id}' needs a human verdict"
        )

    project_root = Path(frozen.workspace_root).resolve()
    if root is not None and Path(root).resolve() != project_root:
        raise ValueError("verification root differs from durable contract workspace_root")
    tools = ToolAdapter(runtime.ledger, execution, root=project_root)
    before_workspace = workspace_fingerprint(
        project_root,
        runtime.ledger.path,
        requirement.artifact_paths,
    )
    _, artifact_records_before, _, _ = _resolve_artifacts(requirement, project_root)
    status = "error"
    exit_code: int | None = None
    stdout, stdout_bytes, stdout_sha256, stdout_truncated = captured_output(None)
    stderr, stderr_bytes, stderr_sha256, stderr_truncated = captured_output(None)
    tool_event_hash: str | None = None
    reason = ""

    try:
        command = tools._run(
            requirement.argv,
            tool="shell",
            action_kind="verification",
            description=(
                f"verify requirement {requirement.id}: {shlex.join(requirement.argv)}"
            ),
            timeout=requirement.timeout_seconds,
            cwd=None,
            mutates_task=False,
            environment_overrides={"PYTHONDONTWRITEBYTECODE": "1"},
            before_effect=before_effect,
        )
        exit_code = command.exit_code
        stdout, stdout_bytes, stdout_sha256, stdout_truncated = captured_output(
            command.stdout
        )
        stderr, stderr_bytes, stderr_sha256, stderr_truncated = captured_output(
            command.stderr
        )
        tool_event_hash = tools.last_event_hash
        if exit_code in requirement.expected_exit_codes:
            status = "pass"
        else:
            status = "fail"
            reason = (
                f"unexpected exit code {exit_code}; expected "
                + ", ".join(str(code) for code in requirement.expected_exit_codes)
            )
    except ActionBlocked as exc:
        status = "blocked"
        reason = str(exc)
    except subprocess.TimeoutExpired as exc:
        status = "timeout"
        stdout, stdout_bytes, stdout_sha256, stdout_truncated = captured_output(exc.stdout)
        stderr, stderr_bytes, stderr_sha256, stderr_truncated = captured_output(exc.stderr)
        reason = f"verification timed out after {requirement.timeout_seconds:g}s"
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        status = "error"
        reason = f"command execution failed: {type(exc).__name__}: {exc}"

    after_workspace = workspace_fingerprint(
        project_root,
        runtime.ledger.path,
        requirement.artifact_paths,
    )
    changed_workspace_paths = workspace_changes(before_workspace, after_workspace)
    workspace_digest = workspace_fingerprint_digest(after_workspace)
    unsafe_mutation = bool(changed_workspace_paths)
    if unsafe_mutation:
        status = "fail"
        summary = ", ".join(changed_workspace_paths[:20])
        if len(changed_workspace_paths) > 20:
            summary += f", ... (+{len(changed_workspace_paths) - 20})"
        reason = "; ".join(
            filter(None, (reason, f"verification changed undeclared workspace paths: {summary}"))
        )

    artifact_hashes, artifact_records, artifact_paths, artifact_problems = _resolve_artifacts(
        requirement,
        project_root,
    )
    if artifact_problems:
        status = "fail" if status == "pass" else status
        reason = "; ".join(filter(None, (reason, *artifact_problems)))

    artifact_state_before = _artifact_state(artifact_records_before)
    artifact_state_after = _artifact_state(artifact_records)
    artifact_changes = sorted(
        path
        for path in artifact_state_before.keys() | artifact_state_after.keys()
        if artifact_state_before.get(path) != artifact_state_after.get(path)
    )
    artifact_mutation_event_hash: str | None = None
    if artifact_changes:
        mutation = runtime.ledger.append(
            AuditEventType.TOOL_CALL,
            {
                "tool": "verification.artifact",
                "requirement_id": requirement.id,
                "paths": artifact_changes,
                "caused_by_tool_event_hash": tool_event_hash,
                "mutates_task": True,
            },
            contract_id=contract.goal_id,
        )
        artifact_mutation_event_hash = mutation.entry_hash

    completed_at = utc_now()
    payload = {
        "requirement_id": requirement.id,
        "manual": False,
        "status": status,
        "argv": list(requirement.argv),
        "expected_exit_codes": list(requirement.expected_exit_codes),
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "stdout_sha256": stdout_sha256,
        "stderr_sha256": stderr_sha256,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "tool_event_hash": tool_event_hash,
        "artifact_records": artifact_records,
        "completed_at": completed_at,
        "reason": reason,
        "workspace_changes": changed_workspace_paths[:100],
        "workspace_fingerprint_sha256": workspace_digest,
        "artifact_mutation_event_hash": artifact_mutation_event_hash,
        "mutates_task": unsafe_mutation,
    }
    event = runtime.record_evidence(
        contract,
        EvidenceKind.VERIFICATION_RESULT,
        payload,
        artifact_paths=artifact_paths,
    )
    if transition_on_failure and (
        status in {"blocked", "timeout", "error"} or unsafe_mutation
    ) and contract.state_value != StateTransition.BLOCKED.value:
        runtime.transition(
            contract,
            StateTransition.BLOCKED,
            f"verification {requirement.id} ended with status {status}",
        )
    return VerificationResult(
        requirement_id=requirement.id,
        status=status,
        argv=requirement.argv,
        expected_exit_codes=requirement.expected_exit_codes,
        exit_code=exit_code,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        artifact_hashes=artifact_hashes,
        completed_at=completed_at,
        event_hash=event.entry_hash,
        reason=reason,
        stdout=stdout,
        stderr=stderr,
        stdout_sha256=stdout_sha256,
        stderr_sha256=stderr_sha256,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )
