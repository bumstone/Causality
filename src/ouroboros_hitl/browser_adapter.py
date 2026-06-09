from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Sequence

from .contracts import AuditEventType
from .ledger import EvidenceLedger

ObserveMode = Literal["interactive", "compact", "full"]
BrowserActionType = Literal["click", "fill", "hover", "press", "select"]

UNTRUSTED_BEGIN = "--- BEGIN UNTRUSTED EXTERNAL CONTENT ---"
UNTRUSTED_END = "--- END UNTRUSTED EXTERNAL CONTENT ---"
REF_RE = re.compile(r"@[ec]\d+")


class BrowserCommandError(RuntimeError):
    def __init__(self, command: Sequence[str], exit_code: int, stderr: str):
        super().__init__(f"browser command failed ({exit_code}): {' '.join(command)}\n{stderr}")
        self.command = list(command)
        self.exit_code = exit_code
        self.stderr = stderr


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class Observation:
    mode: str
    snapshot: str
    state_hash: str
    line_count: int
    ref_count: int
    diff: bool = False
    scope: str | None = None
    artifacts: tuple[str, ...] = ()

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.snapshot) // 4)

    @property
    def untrusted_snapshot(self) -> str:
        return wrap_untrusted(self.snapshot)

    def to_ledger_payload(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "state_hash": self.state_hash,
            "line_count": self.line_count,
            "ref_count": self.ref_count,
            "token_estimate": self.token_estimate,
            "diff": self.diff,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class BrowserAction:
    ref: str
    type: BrowserActionType
    value: str | None = None
    wait_for: str | None = None

    def validate(self) -> None:
        if not REF_RE.fullmatch(self.ref):
            raise ValueError(f"invalid ref '{self.ref}'; expected @eN or @cN")
        if self.type in {"fill", "press", "select"} and self.value is None:
            raise ValueError(f"action '{self.type}' requires a value")

    def to_ledger_payload(self) -> dict[str, str | None]:
        return {
            "ref": self.ref,
            "type": self.type,
            "value": self.value,
            "wait_for": self.wait_for,
        }


Runner = Callable[[Sequence[str]], CommandResult | subprocess.CompletedProcess[str] | str]


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
    """Thin adapter over a snapshot/action browser driver binary.

    The adapter keeps heavy browser state in the driver and returns compact
    observations that can be appended to the Ouroboros ledger.
    """

    def __init__(
        self,
        browse_binary: str | Path | None = None,
        *,
        ledger: EvidenceLedger | None = None,
        artifact_dir: str | Path = ".ouroboros/artifacts",
        runner: Runner | None = None,
    ):
        self.browse_binary = str(browse_binary or self._default_browse_binary())
        self.ledger = ledger
        self.artifact_dir = Path(artifact_dir)
        self.runner = runner

    def observe(
        self,
        mode: ObserveMode = "interactive",
        *,
        scope: str | None = None,
        diff: bool = False,
        annotate: bool = False,
    ) -> Observation:
        command = [self.browse_binary, "snapshot"]
        if mode == "interactive":
            command.append("-i")
        elif mode == "compact":
            command.append("-c")
        elif mode != "full":
            raise ValueError("mode must be interactive, compact, or full")
        if scope:
            command.extend(["-s", scope])
        if diff:
            command.append("-D")
        artifacts: list[str] = []
        if annotate:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            out_path = self.artifact_dir / "snapshot-annotated.png"
            command.extend(["-a", "-o", str(out_path)])
            artifacts.append(str(out_path))

        result = self._run(command)
        snapshot = result.stdout.strip()
        observation = Observation(
            mode=mode,
            snapshot=snapshot,
            state_hash=hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
            line_count=len(snapshot.splitlines()),
            ref_count=len(set(REF_RE.findall(snapshot))),
            diff=diff,
            scope=scope,
            artifacts=tuple(artifacts),
        )
        if self.ledger:
            self.ledger.append(
                AuditEventType.BROWSER_OBSERVATION,
                observation.to_ledger_payload(),
                artifact_paths=artifacts,
            )
        return observation

    def act(self, action: BrowserAction) -> CommandResult:
        action.validate()
        command = [self.browse_binary, action.type, action.ref]
        if action.value is not None:
            command.append(action.value)
        result = self._run(command)
        if self.ledger:
            payload = action.to_ledger_payload()
            payload["exit_code"] = str(result.exit_code)
            self.ledger.append(AuditEventType.BROWSER_ACTION, payload)
        return result

    def assert_state(self, prop: str, target: str) -> CommandResult:
        command = [self.browse_binary, "is", prop, target]
        result = self._run(command)
        if self.ledger:
            self.ledger.append(
                AuditEventType.TOOL_CALL,
                {"tool": "browser.is", "prop": prop, "target": target, "exit_code": result.exit_code},
            )
        return result

    def inspect(self, target: str, kind: Literal["attrs", "html", "css"] = "attrs") -> CommandResult:
        command = [self.browse_binary, kind, target]
        return self._run(command)

    def visual(self, target: str | None = None) -> str:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.artifact_dir / "visual.png"
        if target:
            command = [self.browse_binary, "screenshot", target, str(out_path)]
        else:
            command = [self.browse_binary, "snapshot", "-i", "-a", "-o", str(out_path)]
        self._run(command)
        if self.ledger:
            self.ledger.append(
                AuditEventType.EVIDENCE,
                {"kind": "browser_visual", "path": str(out_path)},
                artifact_paths=[out_path],
            )
        return str(out_path)

    def _run(self, command: Sequence[str]) -> CommandResult:
        if self.runner:
            raw = self.runner(command)
            result = self._coerce_result(raw)
        else:
            completed = subprocess.run(
                list(command),
                text=True,
                capture_output=True,
                check=False,
            )
            result = CommandResult(completed.returncode, completed.stdout, completed.stderr)
        if result.exit_code != 0:
            raise BrowserCommandError(command, result.exit_code, result.stderr)
        return result

    @staticmethod
    def _coerce_result(raw: CommandResult | subprocess.CompletedProcess[str] | str) -> CommandResult:
        if isinstance(raw, CommandResult):
            return raw
        if isinstance(raw, str):
            return CommandResult(0, raw, "")
        return CommandResult(raw.returncode, raw.stdout or "", raw.stderr or "")

    @staticmethod
    def _default_browse_binary() -> Path:
        env_path = os.environ.get("OUROBOROS_BROWSER_BIN")
        if env_path:
            return Path(env_path)
        return Path("browser-driver")
