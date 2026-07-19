from __future__ import annotations

import math
import re
import shlex
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    IRREVERSIBLE = "irreversible"


class StateTransition(str, Enum):
    PLANNED = "planned"
    APPROVED = "approved"
    EXECUTING = "executing"
    BLOCKED = "blocked"
    VERIFIED = "verified"
    REJECTED = "rejected"


class EvidenceKind(str, Enum):
    TEST_OUTPUT = "test_output"
    BROWSER_DIFF = "browser_diff"
    ARTIFACT_HASH = "artifact_hash"
    HUMAN_APPROVAL = "human_approval"
    VERIFIER_PASS = "verifier_pass"
    TOOL_OUTPUT = "tool_output"
    A11Y_REPORT = "a11y_report"
    VERIFICATION_RESULT = "verification_result"


class AuditEventType(str, Enum):
    GOAL_CONTRACT = "goal_contract"
    STATE_TRANSITION = "state_transition"
    TOOL_CALL = "tool_call"
    VERIFIER_DECISION = "verifier_decision"
    HUMAN_DECISION = "human_decision"
    EVIDENCE = "evidence"
    BROWSER_OBSERVATION = "browser_observation"
    BROWSER_ACTION = "browser_action"
    GATE_DECISION = "gate_decision"
    TASK_STARTED = "task_started"
    TASK_OPERATION = "task_operation"
    TASK_ACTION_INTENT = "task_action_intent"
    TASK_ACTION_RESULT = "task_action_result"
    TASK_REFLECTION_INTENT = "task_reflection_intent"
    TASK_REFLECTED = "task_reflected"
    TASK_CONTROLLER_LEASE = "task_controller_lease"
    ORCHESTRATION_ENVIRONMENT = "orchestration_environment"


class GateDecision(str, Enum):
    PASS = "pass"
    REPAIR = "repair"
    ESCALATE = "escalate"
    STOP = "stop"


class ActionType(str, Enum):
    CLICK = "click"
    FILL = "fill"
    HOVER = "hover"
    PRESS = "press"
    SELECT = "select"
    DELETE = "delete"
    DEPLOY = "deploy"
    PAYMENT = "payment"
    EXTERNAL_SEND = "external_send"
    PERMISSION_CHANGE = "permission_change"


IRREVERSIBLE_ACTIONS = {
    ActionType.DELETE.value,
    ActionType.DEPLOY.value,
    ActionType.PAYMENT.value,
    ActionType.EXTERNAL_SEND.value,
    ActionType.PERMISSION_CHANGE.value,
}


@dataclass(frozen=True)
class PermissionContract:
    allowed_tools: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    network_scope: tuple[str, ...] = ()
    auth_scope: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "PermissionContract":
        if not value:
            return cls()
        return cls(
            allowed_tools=tuple(value.get("allowed_tools", ())),
            write_scope=tuple(value.get("write_scope", ())),
            network_scope=tuple(value.get("network_scope", ())),
            auth_scope=tuple(value.get("auth_scope", ())),
        )

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "allowed_tools": list(self.allowed_tools),
            "write_scope": list(self.write_scope),
            "network_scope": list(self.network_scope),
            "auth_scope": list(self.auth_scope),
        }


@dataclass(frozen=True)
class EvidenceRequirement:
    kind: EvidenceKind | str
    description: str
    required: bool = True

    @property
    def kind_value(self) -> str:
        return self.kind.value if isinstance(self.kind, EvidenceKind) else str(self.kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind_value,
            "description": self.description,
            "required": self.required,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EvidenceRequirement":
        return cls(
            kind=value["kind"],
            description=value.get("description", ""),
            required=bool(value.get("required", True)),
        )


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class VerificationRequirement:
    """Executable or explicitly manual completion requirement.

    ``artifact_paths`` maps project-relative paths to an expected SHA-256. A
    ``None`` value requires only that the artifact exists. Keeping the expected
    digest in the frozen contract is what makes a "wrong artifact" decidable;
    hashing only the file produced by the command would merely describe it.
    """

    id: str
    argv: tuple[str, ...]
    expected_exit_codes: tuple[int, ...] = (0,)
    timeout_seconds: float = 30.0
    artifact_paths: Mapping[str, str | None] = field(default_factory=dict)
    required: bool = True
    manual: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.id, str):
            raise ValueError("verification requirement id must be a string")
        if isinstance(self.argv, (str, bytes)) or any(
            not isinstance(item, str) for item in self.argv
        ):
            raise ValueError("argv must be a sequence of strings")
        if not isinstance(self.required, bool) or not isinstance(self.manual, bool):
            raise ValueError("required and manual must be booleans")
        requirement_id = self.id.strip()
        if not requirement_id:
            raise ValueError("verification requirement id must be non-blank")

        argv = tuple(self.argv)
        if self.manual:
            if argv:
                raise ValueError("manual verification requirements cannot declare argv")
        elif not argv or any(not item for item in argv):
            raise ValueError("executable verification requirements need non-empty argv")

        exit_codes = tuple(self.expected_exit_codes)
        if not exit_codes or any(
            not isinstance(code, int) or isinstance(code, bool) for code in exit_codes
        ):
            raise ValueError("expected_exit_codes must contain at least one integer")

        if not isinstance(self.timeout_seconds, (int, float)) or isinstance(
            self.timeout_seconds,
            bool,
        ):
            raise ValueError("timeout_seconds must be a finite number")
        timeout = float(self.timeout_seconds)
        if timeout <= 0 or not math.isfinite(timeout):
            raise ValueError("timeout_seconds must be a finite positive number")

        if not isinstance(self.artifact_paths, Mapping):
            raise ValueError("artifact_paths must be a path-to-sha256 mapping")
        artifacts: dict[str, str | None] = {}
        for raw_path, raw_digest in self.artifact_paths.items():
            if not isinstance(raw_path, str):
                raise ValueError("artifact path keys must be strings")
            path = raw_path.strip()
            if not path:
                raise ValueError("artifact path must be non-blank")
            if raw_digest is not None and not isinstance(raw_digest, str):
                raise ValueError(f"artifact '{path}' expected hash must be a string")
            digest = None if raw_digest is None else raw_digest.strip().lower()
            if digest is not None and not _SHA256_RE.fullmatch(digest):
                raise ValueError(f"artifact '{path}' expected hash must be SHA-256")
            artifacts[path] = digest
        if self.manual and artifacts:
            raise ValueError(
                "manual verification requirements cite evidence artifacts; "
                "they cannot declare artifact_paths"
            )

        object.__setattr__(self, "id", requirement_id)
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "expected_exit_codes", tuple(dict.fromkeys(exit_codes)))
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "artifact_paths", MappingProxyType(artifacts))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "argv": list(self.argv),
            "expected_exit_codes": list(self.expected_exit_codes),
            "timeout_seconds": self.timeout_seconds,
            "artifact_paths": dict(self.artifact_paths),
            "required": self.required,
            "manual": self.manual,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "VerificationRequirement":
        argv = value.get("argv", ())
        if isinstance(argv, (str, bytes)):
            raise ValueError("argv must be a sequence of strings")
        return cls(
            id=value["id"],
            argv=tuple(argv),
            expected_exit_codes=tuple(value.get("expected_exit_codes", (0,))),
            timeout_seconds=value.get("timeout_seconds", 30.0),
            artifact_paths=value.get("artifact_paths", {}),
            required=value.get("required", True),
            manual=value.get("manual", False),
        )


@dataclass(frozen=True)
class VerificationResult:
    """Result returned by a verification execution.

    The ledger event cannot include its own entry hash without creating a
    circular hash. ``event_hash`` is therefore attached to the returned result
    after the evidence event is appended and is the value verifiers cite.
    """

    requirement_id: str
    status: str
    argv: tuple[str, ...]
    expected_exit_codes: tuple[int, ...]
    exit_code: int | None
    stdout_bytes: int
    stderr_bytes: int
    artifact_hashes: Mapping[str, str | None]
    completed_at: str
    event_hash: str
    reason: str = ""
    stdout: str = ""
    stderr: str = ""
    stdout_sha256: str = ""
    stderr_sha256: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_hashes", MappingProxyType(dict(self.artifact_hashes)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "status": self.status,
            "argv": list(self.argv),
            "expected_exit_codes": list(self.expected_exit_codes),
            "exit_code": self.exit_code,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "artifact_hashes": dict(self.artifact_hashes),
            "completed_at": self.completed_at,
            "event_hash": self.event_hash,
            "reason": self.reason,
        }


@dataclass
class GoalContract:
    title: str
    summary: str
    risk: Risk | str = Risk.LOW
    permissions: PermissionContract = field(default_factory=PermissionContract)
    evidence_required: list[EvidenceRequirement] = field(default_factory=list)
    non_goals: tuple[str, ...] = ()
    state: StateTransition | str = StateTransition.PLANNED
    stopping_policy: dict[str, Any] = field(
        default_factory=lambda: {
            "max_iterations": 5,
            "max_failed_hypotheses": 3,
            "no_progress_iterations": 2,
        }
    )
    goal_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=utc_now)
    verification_requirements: tuple[VerificationRequirement, ...] = ()
    workspace_root: str = ""

    def __post_init__(self) -> None:
        try:
            self.risk = Risk(self.risk)
        except (TypeError, ValueError) as exc:
            allowed = ", ".join(item.value for item in Risk)
            raise ValueError(f"risk must be one of: {allowed}") from exc
        requirements = tuple(self.verification_requirements)
        ids = [item.id for item in requirements]
        duplicates = sorted(item for item, count in Counter(ids).items() if count > 1)
        if duplicates:
            raise ValueError(
                "verification requirement ids must be unique: " + ", ".join(duplicates)
            )
        if requirements and not any(item.required for item in requirements):
            raise ValueError("at least one structured verification requirement must be required")
        self.verification_requirements = requirements

    @property
    def risk_value(self) -> str:
        return self.risk.value if isinstance(self.risk, Risk) else str(self.risk)

    @property
    def state_value(self) -> str:
        return self.state.value if isinstance(self.state, StateTransition) else str(self.state)

    @property
    def approval_required(self) -> bool:
        return self.risk_value in {Risk.HIGH.value, Risk.IRREVERSIBLE.value}

    def required_evidence_kinds(self) -> set[str]:
        return {item.kind_value for item in self.evidence_required if item.required}

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "title": self.title,
            "summary": self.summary,
            "risk": self.risk_value,
            "permissions": self.permissions.to_dict(),
            "evidence_required": [item.to_dict() for item in self.evidence_required],
            "verification_requirements": [
                item.to_dict() for item in self.verification_requirements
            ],
            "workspace_root": self.workspace_root,
            "non_goals": list(self.non_goals),
            "state": self.state_value,
            "stopping_policy": dict(self.stopping_policy),
            "created_at": self.created_at,
            "approval_required": self.approval_required,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GoalContract":
        return cls(
            goal_id=value.get("goal_id", str(uuid4())),
            title=value["title"],
            summary=value.get("summary", ""),
            risk=value.get("risk", Risk.LOW.value),
            permissions=PermissionContract.from_mapping(value.get("permissions")),
            evidence_required=[
                EvidenceRequirement.from_mapping(item)
                for item in value.get("evidence_required", [])
            ],
            verification_requirements=tuple(
                VerificationRequirement.from_mapping(item)
                for item in value.get("verification_requirements", [])
            ),
            workspace_root=str(value.get("workspace_root", "")),
            non_goals=tuple(value.get("non_goals", ())),
            state=value.get("state", StateTransition.PLANNED.value),
            stopping_policy=dict(value.get("stopping_policy", {})),
            created_at=value.get("created_at", utc_now()),
        )


@dataclass(frozen=True)
class VerifierDecision:
    verifier: str
    status: str
    rationale: str
    severity: str = "normal"
    evidence_refs: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)

    @property
    def is_pass(self) -> bool:
        return self.status == "pass"

    @property
    def is_critical_failure(self) -> bool:
        return self.status == "fail" and self.severity == "critical"

    def _has_evidence(self) -> bool:
        """True if at least one ``evidence_ref`` is a real (non-blank) citation.

        A blank placeholder like ``("",)`` is not evidence -- counting it would
        let a hollow verifier satisfy even the ``require_evidence`` bar."""
        return any(ref and ref.strip() for ref in self.evidence_refs)

    @property
    def is_substantive(self) -> bool:
        """A verdict carries substance when it cites evidence or states a
        rationale. One that does neither is a hollow rubber-stamp -- not a
        verification -- so it must not count toward the independent-pass quorum
        (the ledger's claims-vs-evidence rule, applied to the gate)."""
        return self._has_evidence() or bool(self.rationale and self.rationale.strip())

    def counts_as_pass(self, *, require_evidence: bool = False) -> bool:
        """Whether this verdict counts toward the completion quorum.

        A counted pass must be a pass AND substantive. With ``require_evidence``
        the bar rises to citing at least one *non-blank* ``evidence_ref`` -- prose
        alone is a claim, not evidence -- which a high-assurance completion can
        demand.
        """
        if not self.is_pass:
            return False
        if require_evidence:
            return self._has_evidence()
        return self.is_substantive

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_refs"] = list(self.evidence_refs)
        return value


def _derive_escalation(contract: "GoalContract") -> tuple[str, ...]:
    """Escalation triggers as a *derived view* of the gate's risk-based
    behavior, not a separately enforced field. The HITLGate stays the single
    enforcer, which avoids the dual-model conflict (ADR 0006, C-ESC-1)."""
    triggers: list[str] = ["irreversible_action_approval"]
    if contract.approval_required:
        triggers.insert(0, "high_risk_plan_approval")
        triggers.append("final_approval")
    return tuple(triggers)


@dataclass(frozen=True)
class TaskContract:
    """Immutable, binding rules-of-engagement derived from a GoalContract.

    This is not a new goal specification; it is a frozen *view* of the clauses
    a run must honor (ADR 0001). It narrows scope (``non_goals``,
    ``allowed_tools``) rather than widening it, and cannot be mutated mid-loop.
    """

    objective: str
    non_goals: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    stop_condition: Mapping[str, Any]
    verification: tuple[str, ...]
    escalation: tuple[str, ...]
    goal_id: str = ""
    verification_requirements: tuple[VerificationRequirement, ...] = ()
    workspace_root: str = ""

    @classmethod
    def of(cls, contract: "GoalContract") -> "TaskContract":
        summary = contract.summary.strip()
        objective = f"{contract.title}: {summary}" if summary else contract.title
        verification = tuple(
            item.description or item.kind_value
            for item in contract.evidence_required
            if item.required
        )
        verification += tuple(
            item.id if item.manual else shlex.join(item.argv)
            for item in contract.verification_requirements
            if item.required
        )
        return cls(
            objective=objective,
            non_goals=tuple(contract.non_goals),
            allowed_tools=tuple(contract.permissions.allowed_tools),
            stop_condition=MappingProxyType(dict(contract.stopping_policy)),
            verification=verification,
            escalation=_derive_escalation(contract),
            goal_id=contract.goal_id,
            verification_requirements=tuple(contract.verification_requirements),
            workspace_root=contract.workspace_root,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "non_goals": list(self.non_goals),
            "allowed_tools": list(self.allowed_tools),
            "stop_condition": dict(self.stop_condition),
            "verification": list(self.verification),
            "escalation": list(self.escalation),
            "goal_id": self.goal_id,
            "verification_requirements": [
                item.to_dict() for item in self.verification_requirements
            ],
            "workspace_root": self.workspace_root,
        }


def contract_binding_payload(contract: GoalContract) -> dict[str, Any]:
    """Immutable clauses whose durable snapshot must govern execution."""
    value = contract.to_dict()
    value.pop("state", None)
    value.pop("approval_required", None)
    return value
