from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Sequence

ObserveMode = Literal["interactive", "compact", "full"]
BrowserActionType = Literal["click", "fill", "hover", "press", "select"]
BrowserAssertion = Literal["visible", "enabled", "checked"]
BrowserInspection = Literal["attrs", "html", "css"]

BROWSER_PROTOCOL_VERSION = 1
REQUIRED_BROWSER_OPERATIONS = frozenset(
    {"observe", "act", "assert", "inspect", "visual", "console", "network"}
)
ASSERTION_PROPERTIES = frozenset({"visible", "enabled", "checked"})
INSPECTION_KINDS = frozenset({"attrs", "html", "css"})
UNTRUSTED_BEGIN = "--- BEGIN UNTRUSTED EXTERNAL CONTENT ---"
UNTRUSTED_END = "--- END UNTRUSTED EXTERNAL CONTENT ---"
REF_RE = re.compile(r"@[ec]\d+")
SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class BrowserCommandError(RuntimeError):
    """A driver failure whose message never echoes page data or action values."""

    def __init__(self, operation: str, exit_code: int, stderr: str = ""):
        super().__init__(f"browser operation '{operation}' failed with exit code {exit_code}")
        self.operation = operation
        self.exit_code = exit_code
        self.stderr_bytes = len(stderr.encode("utf-8"))
        self.stderr_sha256 = hashlib.sha256(stderr.encode("utf-8")).hexdigest()


class BrowserOutputLimitError(RuntimeError):
    def __init__(self, operation: str, output_bytes: int, limit: int):
        super().__init__(
            f"browser operation '{operation}' produced {output_bytes} bytes; limit is {limit}"
        )
        self.operation = operation
        self.output_bytes = output_bytes
        self.limit = limit


class BrowserInputLimitError(RuntimeError):
    def __init__(self, input_bytes: int, limit: int):
        super().__init__(f"browser action input is {input_bytes} bytes; limit is {limit}")
        self.input_bytes = input_bytes
        self.limit = limit


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str | bytes = ""
    stderr: str | bytes = ""


@dataclass(frozen=True)
class BrowserCapabilities:
    protocol_version: int
    session_isolation: bool
    network_scope_enforcement: bool
    operations: frozenset[str]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BrowserCapabilities":
        protocol = value.get("protocol_version")
        isolation = value.get("session_isolation")
        network_policy = value.get("network_scope_enforcement")
        operations = value.get("operations")
        if protocol != BROWSER_PROTOCOL_VERSION:
            raise ValueError(
                f"browser driver protocol must be {BROWSER_PROTOCOL_VERSION}"
            )
        if isolation is not True:
            raise ValueError("browser driver must guarantee task session isolation")
        if network_policy is not True:
            raise ValueError("browser driver must enforce exact-origin network scope")
        if (
            not isinstance(operations, list)
            or any(not isinstance(item, str) or not item for item in operations)
        ):
            raise ValueError("browser driver operations must be a string array")
        normalized = frozenset(operations)
        missing = REQUIRED_BROWSER_OPERATIONS - normalized
        if missing:
            raise ValueError(
                "browser driver is missing required operations: "
                + ", ".join(sorted(missing))
            )
        return cls(protocol, isolation, network_policy, normalized)


@dataclass(frozen=True)
class BrowserContext:
    session_id: str
    profile_dir: str | Path
    allowed_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not SESSION_RE.fullmatch(
            self.session_id
        ):
            raise ValueError("browser session_id is invalid")
        if not str(self.profile_dir):
            raise ValueError("browser profile_dir must be non-blank")
        if any(
            not isinstance(origin, str) or not origin.strip()
            for origin in self.allowed_origins
        ):
            raise ValueError("browser allowed_origins must contain non-blank strings")
        object.__setattr__(self, "allowed_origins", tuple(self.allowed_origins))


@dataclass(frozen=True)
class BrowserArtifact:
    path: str
    bytes: int
    sha256: str

    def to_metadata(self) -> dict[str, object]:
        return {"path": self.path, "bytes": self.bytes, "sha256": self.sha256}


@dataclass(frozen=True)
class Observation:
    mode: str
    snapshot: str
    state_hash: str
    line_count: int
    ref_count: int
    diff: bool = False
    scope: str | None = None
    artifacts: tuple[BrowserArtifact, ...] = ()

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.snapshot) // 4)

    @property
    def untrusted_snapshot(self) -> str:
        return wrap_untrusted(self.snapshot)

    def to_metadata(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "state_hash": self.state_hash,
            "snapshot_bytes": len(self.snapshot.encode("utf-8")),
            "line_count": self.line_count,
            "ref_count": self.ref_count,
            "token_estimate": self.token_estimate,
            "diff": self.diff,
            "scope": self.scope,
            "artifacts": [artifact.to_metadata() for artifact in self.artifacts],
        }


@dataclass(frozen=True)
class BrowserAction:
    ref: str
    type: BrowserActionType
    value: str | None = None

    def validate(self) -> None:
        _stable_ref(self.ref, "ref")
        if self.type not in {"click", "fill", "hover", "press", "select"}:
            raise ValueError(f"unknown browser action '{self.type}'")
        if self.type in {"fill", "press", "select"} and self.value is None:
            raise ValueError(f"action '{self.type}' requires a value")
        if self.value is not None and not isinstance(self.value, str):
            raise ValueError("browser action value must be text")
        if self.type in {"click", "hover"} and self.value is not None:
            raise ValueError(f"action '{self.type}' does not accept a value")


@dataclass(frozen=True)
class BrowserDeltas:
    console: str
    network: str

    @property
    def console_sha256(self) -> str:
        return hashlib.sha256(self.console.encode("utf-8")).hexdigest()

    @property
    def network_sha256(self) -> str:
        return hashlib.sha256(self.network.encode("utf-8")).hexdigest()

    def to_metadata(self) -> dict[str, object]:
        return {
            "console_bytes": len(self.console.encode("utf-8")),
            "console_sha256": self.console_sha256,
            "network_bytes": len(self.network.encode("utf-8")),
            "network_sha256": self.network_sha256,
        }


Runner = Callable[
    [Sequence[str], Mapping[str, str]],
    CommandResult | subprocess.CompletedProcess[str] | str,
]


def _stable_ref(value: str, name: str) -> str:
    if not isinstance(value, str) or not REF_RE.fullmatch(value):
        raise ValueError(f"invalid {name} '{value}'; expected @eN or @cN")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wrap_untrusted(value: str) -> str:
    return f"{UNTRUSTED_BEGIN}\n{value}\n{UNTRUSTED_END}"


def compression_stats(full_snapshot: str, compact_snapshot: str) -> dict[str, float | int]:
    full_chars = max(1, len(full_snapshot))
    compact_chars = len(compact_snapshot)
    return {
        "full_chars": len(full_snapshot),
        "compact_chars": compact_chars,
        "char_compression_ratio": round(compact_chars / full_chars, 4),
        "full_lines": len(full_snapshot.splitlines()),
        "compact_lines": len(compact_snapshot.splitlines()),
    }


class A11yBrowserAdapter:
    """Pure, bounded adapter over a host-owned browser wrapper command."""

    def __init__(
        self,
        browse_binary: str | Path | Sequence[str] | None = None,
        *,
        runner: Runner | None = None,
        timeout_seconds: float = 30.0,
        max_action_value_bytes: int = 64 * 1024,
        max_output_bytes: int = 1024 * 1024,
        max_artifact_bytes: int = 16 * 1024 * 1024,
    ):
        self.browser_command = self._coerce_command(
            browse_binary if browse_binary is not None else self._default_browse_command()
        )
        self.browse_binary = self.browser_command[0]
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        for name, value in (
            ("max_action_value_bytes", max_action_value_bytes),
            ("max_output_bytes", max_output_bytes),
            ("max_artifact_bytes", max_artifact_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.runner = runner
        self.timeout_seconds = float(timeout_seconds)
        self.max_action_value_bytes = max_action_value_bytes
        self.max_output_bytes = max_output_bytes
        self.max_artifact_bytes = max_artifact_bytes
        self._capability_cache: BrowserCapabilities | None = None

    def capabilities(self) -> BrowserCapabilities:
        if self._capability_cache is None:
            result = self._run(("capabilities", "--json"))
            try:
                raw = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise ValueError("browser capability response must be JSON") from exc
            if not isinstance(raw, Mapping):
                raise ValueError("browser capability response must be an object")
            self._capability_cache = BrowserCapabilities.from_mapping(raw)
        return self._capability_cache

    def observe(
        self,
        mode: ObserveMode = "interactive",
        *,
        scope: str | None = None,
        diff: bool = False,
        annotate_path: str | Path | None = None,
        context: BrowserContext | None = None,
    ) -> Observation:
        command: list[str] = ["snapshot"]
        if mode == "interactive":
            command.append("-i")
        elif mode == "compact":
            command.append("-c")
        elif mode != "full":
            raise ValueError("mode must be interactive, compact, or full")
        if scope is not None:
            command.extend(("-s", _stable_ref(scope, "scope")))
        if diff:
            command.append("-D")

        artifacts: tuple[BrowserArtifact, ...] = ()
        if annotate_path is None:
            result = self._run(
                command,
                context=context,
            )
        else:
            result, artifact = self._run_artifact(
                command + ["-a", "-o"],
                Path(annotate_path),
                context=context,
            )
            artifacts = (artifact,)
        snapshot = result.stdout.strip()
        return Observation(
            mode=mode,
            snapshot=snapshot,
            state_hash=hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
            line_count=len(snapshot.splitlines()),
            ref_count=len(set(REF_RE.findall(snapshot))),
            diff=diff,
            scope=scope,
            artifacts=artifacts,
        )

    def act(
        self,
        action: BrowserAction,
        *,
        context: BrowserContext | None = None,
    ) -> CommandResult:
        action.validate()
        value_bytes = len((action.value or "").encode("utf-8"))
        if value_bytes > self.max_action_value_bytes:
            raise BrowserInputLimitError(value_bytes, self.max_action_value_bytes)
        command = [action.type, action.ref]
        if action.value is not None:
            command.append(action.value)
        return self._run(command, context=context)

    def assert_state(
        self,
        prop: BrowserAssertion,
        target: str,
        *,
        context: BrowserContext | None = None,
    ) -> CommandResult:
        if prop not in ASSERTION_PROPERTIES:
            raise ValueError("unsupported browser assertion")
        return self._run(
            ("is", prop, _stable_ref(target, "target")),
            context=context,
        )

    def inspect(
        self,
        target: str,
        kind: BrowserInspection = "attrs",
        *,
        context: BrowserContext | None = None,
    ) -> CommandResult:
        if kind not in INSPECTION_KINDS:
            raise ValueError("unsupported browser inspection")
        return self._run(
            (kind, _stable_ref(target, "target")),
            context=context,
        )

    def diagnostics(
        self,
        *,
        context: BrowserContext | None = None,
    ) -> BrowserDeltas:
        console = self._run(
            ("console",), context=context
        ).stdout.strip()
        network = self._run(
            ("network",), context=context
        ).stdout.strip()
        return BrowserDeltas(console=console, network=network)

    def visual(
        self,
        artifact_path: str | Path,
        *,
        target_ref: str | None = None,
        context: BrowserContext | None = None,
    ) -> BrowserArtifact:
        command = ["screenshot"]
        if target_ref is not None:
            command.append(_stable_ref(target_ref, "target_ref"))
        _result, artifact = self._run_artifact(
            command,
            Path(artifact_path),
            context=context,
        )
        return artifact

    def _run_artifact(
        self,
        command: Sequence[str],
        target: Path,
        *,
        context: BrowserContext | None,
    ) -> tuple[CommandResult, BrowserArtifact]:
        if not target.parent.is_dir():
            raise ValueError("browser artifact parent must already exist")
        descriptor, temp_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        os.close(descriptor)
        temporary = Path(temp_name)
        try:
            result = self._run(
                (*command, str(temporary)),
                context=context,
            )
            status = temporary.lstat()
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise ValueError("browser artifact must be a private regular file")
            if status.st_size > self.max_artifact_bytes:
                raise BrowserOutputLimitError(
                    command[0], status.st_size, self.max_artifact_bytes
                )
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            try:
                target.chmod(0o600)
            except OSError:
                pass
            return result, BrowserArtifact(
                path=str(target),
                bytes=target.stat().st_size,
                sha256=_sha256_file(target),
            )
        finally:
            temporary.unlink(missing_ok=True)

    def _run(
        self,
        command: Sequence[str],
        *,
        context: BrowserContext | None = None,
    ) -> CommandResult:
        operation = command[0] if command else "unknown"
        argv = (*self.browser_command, *command)
        environment = self._driver_environment(context)
        if self.runner:
            raw = self.runner(argv, environment)
            result = self._coerce_result(raw)
        else:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                try:
                    completed = subprocess.run(
                        list(argv),
                        stdout=stdout,
                        stderr=stderr,
                        check=False,
                        env=environment,
                        timeout=self.timeout_seconds,
                    )
                except subprocess.TimeoutExpired as exc:
                    stderr.seek(0)
                    raw_stderr = stderr.read(self.max_output_bytes)
                    raise BrowserCommandError(
                        operation,
                        -1,
                        raw_stderr.decode("utf-8", errors="replace"),
                    ) from exc
                stdout_bytes = stdout.tell()
                stderr_bytes = stderr.tell()
                if stdout_bytes + stderr_bytes > self.max_output_bytes:
                    raise BrowserOutputLimitError(
                        operation,
                        stdout_bytes + stderr_bytes,
                        self.max_output_bytes,
                    )
                stdout.seek(0)
                stderr.seek(0)
                result = CommandResult(
                    completed.returncode,
                    stdout.read().decode("utf-8", errors="replace"),
                    stderr.read().decode("utf-8", errors="replace"),
                )
        output_bytes = len(result.stdout.encode("utf-8")) + len(
            result.stderr.encode("utf-8")
        )
        if output_bytes > self.max_output_bytes:
            raise BrowserOutputLimitError(
                operation, output_bytes, self.max_output_bytes
            )
        if result.exit_code != 0:
            raise BrowserCommandError(operation, result.exit_code, result.stderr)
        return result

    @staticmethod
    def _driver_environment(
        context: BrowserContext | None,
    ) -> Mapping[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("CAUSALITY_")
        }
        if context is not None:
            environment["CAUSALITY_BROWSER_SESSION_ID"] = context.session_id
            environment["CAUSALITY_BROWSER_PROFILE_DIR"] = str(context.profile_dir)
            environment["CAUSALITY_BROWSER_ALLOWED_ORIGINS_JSON"] = json.dumps(
                list(context.allowed_origins),
                ensure_ascii=True,
                separators=(",", ":"),
            )
        return MappingProxyType(environment)

    @staticmethod
    def _coerce_result(
        raw: CommandResult | subprocess.CompletedProcess[str] | str,
    ) -> CommandResult:
        if isinstance(raw, CommandResult):
            return raw
        if isinstance(raw, str):
            return CommandResult(0, raw, "")
        return CommandResult(raw.returncode, raw.stdout or "", raw.stderr or "")

    @staticmethod
    def _coerce_command(value: str | Path | Sequence[str]) -> tuple[str, ...]:
        if isinstance(value, (str, Path)):
            command = (str(value),)
        else:
            command = tuple(value)
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("browser command must contain non-empty argv strings")
        return command

    @staticmethod
    def _default_browse_command() -> tuple[str, ...]:
        raw_command = os.environ.get("CAUSALITY_BROWSER_COMMAND_JSON")
        if raw_command:
            value = json.loads(raw_command)
            if not isinstance(value, list):
                raise ValueError("CAUSALITY_BROWSER_COMMAND_JSON must be an argv array")
            return A11yBrowserAdapter._coerce_command(value)
        env_path = os.environ.get("CAUSALITY_BROWSER_BIN")
        return (env_path or "browser-driver",)
