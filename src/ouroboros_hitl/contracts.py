from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
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


@dataclass
class GoalContract:
    title: str
    summary: str
    risk: Risk | str = Risk.LOW
    permissions: PermissionContract = field(default_factory=PermissionContract)
    evidence_required: list[EvidenceRequirement] = field(default_factory=list)
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

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_refs"] = list(self.evidence_refs)
        return value
