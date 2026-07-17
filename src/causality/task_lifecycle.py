"""Read-only, event-sourced projection for durable MCP task sessions.

The ledger is the only source of truth.  This module deliberately does not
execute effects or append recovery events: an effect intent without a matching
result is reported as blocked until a trusted caller records its resolution.
"""

from __future__ import annotations

import hashlib
import difflib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5

from .agent_harness import AgentHarness
from .contracts import (
    AuditEventType,
    EvidenceKind,
    GateDecision,
    GoalContract,
    IRREVERSIBLE_ACTIONS,
    PermissionContract,
    StateTransition,
    VerifierDecision,
    utc_now,
)
from .browser_adapter import (
    A11yBrowserAdapter,
    ASSERTION_PROPERTIES,
    BrowserAction,
    BrowserContext,
    INSPECTION_KINDS,
    REF_RE,
)
from .durable import file_lock
from .execution import ActionBlocked, ExecutionAdapter
from .gates import GateResult
from .http_adapter import (
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    HttpAdapter,
    HttpRequest,
    normalize_origin,
)
from .ledger import EvidenceLedger, LedgerEvent, sha256_file, sha256_text
from .orchestrator import Causality
from .memory import TypedMemory
from .playbooks import build_phase_plan, resolve_playbooks
from .reflect import reflect_on_contract
from .tool_adapter import ToolAdapter, utf8_size


SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_BROWSER_TOOL_BY_OPERATION = MappingProxyType(
    {
        "observe": "browser.observe",
        "act": "browser.act",
        "assert": "browser.assert",
        "inspect": "browser.inspect",
        "visual": "browser.visual",
    }
)
_BROWSER_TOOLS = frozenset(_BROWSER_TOOL_BY_OPERATION.values())
_BROWSER_MODES = frozenset({"interactive", "compact", "full"})
_MAX_BROWSER_CACHE_BYTES = 8 * 1024 * 1024
_browser_mkstemp = tempfile.mkstemp
_CALLER_FORBIDDEN_HTTP_HEADERS = frozenset(
    {
        "authorization",
        "api-key",
        "connection",
        "content-length",
        "cookie",
        "host",
        "proxy-authorization",
        "proxy-connection",
        "set-cookie",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "x-access-token",
        "x-api-key",
        "x-auth-token",
        "x-request-secret",
    }
)

TASK_STARTED = "task_started"
TASK_OPERATION = "task_operation"
TASK_ACTION_INTENT = "task_action_intent"
TASK_ACTION_RESULT = "task_action_result"
TASK_REFLECTION_INTENT = "task_reflection_intent"
TASK_REFLECTED = "task_reflected"
GOAL_CONTRACT = "goal_contract"
STATE_TRANSITION = "state_transition"

_TASK_EVENTS = frozenset(
    {
        TASK_STARTED,
        TASK_OPERATION,
        TASK_ACTION_INTENT,
        TASK_ACTION_RESULT,
        TASK_REFLECTION_INTENT,
        TASK_REFLECTED,
    }
)


def canonical_sha256(value: Any) -> str:
    """Return the SHA-256 of canonical JSON, rejecting non-JSON numbers."""

    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _idempotency_key(value: str) -> str:
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
        raise _error(
            "invalid_idempotency_key",
            "idempotency_key must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
        )
    return value


def _contract_request(contract: GoalContract) -> dict[str, Any]:
    value = contract.to_dict()
    for name in ("goal_id", "created_at", "workspace_root", "state", "approval_required"):
        value.pop(name, None)
    return value


def _workflow_begin_request(
    contract: GoalContract,
    workflow: str,
    phase_plan: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    request = _contract_request(contract)
    permissions = request.get("permissions")
    if isinstance(permissions, Mapping):
        normalized = dict(permissions)
        try:
            normalized["network_scope"] = [
                normalize_origin(origin, scope=True)
                for origin in normalized.get("network_scope", ())
            ]
        except (TypeError, ValueError) as exc:
            raise _error(
                "validation_error",
                "network_scope must contain exact HTTP origins",
            ) from exc
        request["permissions"] = normalized
    request.update({"workflow": workflow, "phase_plan": list(phase_plan)})
    return request


_PHASE_PLAN_FIELDS = frozenset(
    {
        "phase_id",
        "playbook",
        "name",
        "steps",
        "requires_action",
        "requires_verification",
        "requires_verdicts",
    }
)


def _workflow_snapshot(
    contract: GoalContract,
    workflow: str,
) -> tuple[dict[str, Any], ...]:
    if workflow == "legacy":
        return ()
    if workflow == "root-cause-protocol":
        playbooks = resolve_playbooks((workflow,))
    elif workflow == "auto":
        harness = AgentHarness()
        dispatch = harness.route(
            harness.classify(f"{contract.title}\n{contract.summary}")
        )
        playbooks = harness.playbooks(dispatch)
    else:
        raise _error(
            "validation_error",
            "workflow must be legacy, auto, or root-cause-protocol",
        )
    return build_phase_plan(playbooks)


def _initial_workflow_phases(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        raise _error("invalid_task_event", "task_started.phase_plan must be an array")
    phases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != _PHASE_PLAN_FIELDS:
            raise _error("invalid_task_event", "workflow phase snapshot is invalid")
        phase_id = raw.get("phase_id")
        playbook = raw.get("playbook")
        name = raw.get("name")
        steps = raw.get("steps")
        action = raw.get("requires_action")
        verification = raw.get("requires_verification")
        verdicts = raw.get("requires_verdicts")
        if (
            not isinstance(phase_id, str)
            or not phase_id.strip()
            or not isinstance(playbook, str)
            or not playbook.strip()
            or not isinstance(name, str)
            or not name.strip()
            or phase_id != f"{playbook}/{name}"
            or phase_id in seen
            or not isinstance(steps, (list, tuple))
            or not steps
            or any(not isinstance(step, str) or not step.strip() for step in steps)
            or not isinstance(action, bool)
            or not isinstance(verification, bool)
            or isinstance(verdicts, bool)
            or not isinstance(verdicts, int)
            or verdicts < 1
        ):
            raise _error("invalid_task_event", "workflow phase snapshot is invalid")
        seen.add(phase_id)
        phases.append(
            {
                **dict(raw),
                "steps": tuple(steps),
                "status": "pending",
                "attempt": 0,
                "evidence_hashes": (),
                "start_position": -1,
            }
        )
    return phases


def _current_workflow_phase(phases: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (phase for phase in phases if phase["status"] == "running"),
        next((phase for phase in phases if phase["status"] != "passed"), None),
    )


def _verification_result_requires_phase_block(
    event: LedgerEvent,
    events_by_hash: Mapping[str, LedgerEvent],
) -> bool:
    if (
        event.event_type != TASK_ACTION_RESULT
        or event.payload.get("operation") != "verify"
    ):
        return False
    result = event.payload.get("result")
    provenance = event.payload.get("provenance_event_hashes")
    if not isinstance(result, Mapping) or not isinstance(provenance, list):
        return False
    return result.get("status") in {"blocked", "timeout", "error"} or any(
        events_by_hash.get(ref) is not None
        and events_by_hash[ref].payload.get("mutates_task") is True
        for ref in provenance
        if isinstance(ref, str)
    )


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass
class TaskLifecycleError(Exception):
    """Stable error envelope suitable for JSON-RPC adapters."""

    code: str
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.details = _freeze(dict(self.details))
        self.args = (self.message,)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": _thaw(self.details),
        }


class TaskState(str, Enum):
    PLANNED = "planned"
    APPROVED = "approved"
    EXECUTING = "executing"
    BLOCKED = "blocked"
    VERIFIED = "verified"
    REJECTED = "rejected"


@dataclass(frozen=True)
class WorkflowPhase:
    phase_id: str
    playbook: str
    name: str
    steps: tuple[str, ...]
    requires_action: bool
    requires_verification: bool
    requires_verdicts: int
    status: str = "pending"
    attempt: int = 0
    evidence_hashes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "playbook": self.playbook,
            "name": self.name,
            "steps": list(self.steps),
            "requires_action": self.requires_action,
            "requires_verification": self.requires_verification,
            "requires_verdicts": self.requires_verdicts,
            "status": self.status,
            "attempt": self.attempt,
            "evidence_hashes": list(self.evidence_hashes),
        }


@dataclass(frozen=True)
class TaskPolicy:
    """Server-owned ceiling; a task contract may only narrow it."""

    allowed_tools: frozenset[str] = frozenset({"shell", "file.read", "file.write"})
    subprocess_argv_prefixes: tuple[tuple[str, ...], ...] = ()
    verification_commands: tuple[tuple[str, ...], ...] = ()
    verification_argv_prefixes: tuple[tuple[str, ...], ...] = ()
    max_timeout_seconds: float = 300.0
    allowed_network_origins: frozenset[str] = frozenset()
    allowed_auth_refs: frozenset[str] = frozenset()
    allowed_http_headers: frozenset[str] = frozenset()
    max_http_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    max_http_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        try:
            origins = frozenset(
                normalize_origin(origin, scope=True)
                for origin in self.allowed_network_origins
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("allowed_network_origins must contain exact HTTP origins") from exc
        if any(
            not isinstance(ref, str) or not _IDEMPOTENCY_KEY.fullmatch(ref)
            for ref in self.allowed_auth_refs
        ):
            raise ValueError("allowed_auth_refs must contain non-blank credential aliases")
        try:
            header_names = tuple(self.allowed_http_headers)
            HttpRequest(
                "GET",
                "https://header-validation.invalid",
                headers={name: "value" for name in header_names},
            )
            normalized_headers = frozenset(name.casefold() for name in header_names)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("allowed_http_headers must contain valid header names") from exc
        if len(normalized_headers) != len(header_names):
            raise ValueError("allowed_http_headers contains duplicate header names")
        if normalized_headers & _CALLER_FORBIDDEN_HTTP_HEADERS:
            raise ValueError("credential headers cannot be caller-allowed")
        if (
            isinstance(self.max_http_request_bytes, bool)
            or not isinstance(self.max_http_request_bytes, int)
            or self.max_http_request_bytes < 0
        ):
            raise ValueError("max_http_request_bytes must be a non-negative integer")
        if (
            isinstance(self.max_http_response_bytes, bool)
            or not isinstance(self.max_http_response_bytes, int)
            or self.max_http_response_bytes < 0
        ):
            raise ValueError("max_http_response_bytes must be a non-negative integer")
        object.__setattr__(self, "allowed_network_origins", origins)
        object.__setattr__(self, "allowed_auth_refs", frozenset(self.allowed_auth_refs))
        object.__setattr__(self, "allowed_http_headers", normalized_headers)

    def allows_subprocess(self, argv: tuple[str, ...]) -> bool:
        return any(
            len(argv) >= len(prefix) and argv[: len(prefix)] == prefix
            for prefix in self.subprocess_argv_prefixes
        )

    def allows_verification(self, argv: tuple[str, ...]) -> bool:
        return argv in self.verification_commands or any(
            len(argv) >= len(prefix) and argv[: len(prefix)] == prefix
            for prefix in self.verification_argv_prefixes
        )


EffectRunner = Callable[[dict[str, Any]], Mapping[str, Any]]
ApprovalAuthorizer = Callable[[str, str, str | None], bool]


@dataclass(frozen=True)
class _ActionPlan:
    descriptor: dict[str, Any]
    tool: str
    action_kind: str
    description: str
    network_origin: str | None = None
    auth_ref: str | None = None
    http_request: HttpRequest | None = None
    expected_statuses: tuple[int, ...] = ()
    browser: "_BrowserPlan | None" = None


@dataclass(frozen=True)
class _BrowserPlan:
    operation: str
    mode: str = "interactive"
    scope: str | None = None
    annotate: bool = False
    action: str | None = None
    ref: str | None = None
    value: str | None = None
    expected_state_hash: str | None = None
    assertion: str | None = None
    inspection: str | None = None


_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = MappingProxyType(
    {
        TaskState.PLANNED: frozenset(
            {TaskState.APPROVED, TaskState.EXECUTING, TaskState.REJECTED}
        ),
        TaskState.APPROVED: frozenset(
            {TaskState.EXECUTING, TaskState.BLOCKED, TaskState.REJECTED}
        ),
        TaskState.EXECUTING: frozenset(
            {TaskState.VERIFIED, TaskState.BLOCKED, TaskState.REJECTED}
        ),
        TaskState.BLOCKED: frozenset({TaskState.EXECUTING, TaskState.REJECTED}),
        TaskState.VERIFIED: frozenset(),
        TaskState.REJECTED: frozenset(),
    }
)


@dataclass(frozen=True)
class IdempotencyRecord:
    operation: str
    idempotency_key: str
    request_sha256: str
    operation_id: str
    request: Any = None
    response: Any = None
    outcome: str | None = None
    event_hashes: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.outcome is not None

@dataclass(frozen=True)
class PendingIntent:
    kind: str
    operation: str
    operation_id: str
    idempotency_key: str
    request_sha256: str
    descriptor: Any
    event_hash: str


@dataclass(frozen=True)
class TaskSession:
    schema_version: int
    task_id: str
    contract_id: str
    contract_snapshot: Mapping[str, Any]
    contract_hash: str
    state: TaskState
    phase: str
    requirement_results: Mapping[str, Any]
    idempotency: Mapping[tuple[str, str], IdempotencyRecord]
    unresolved_intents: tuple[PendingIntent, ...]
    reflection: Mapping[str, Any] | None
    event_hashes: tuple[str, ...]
    terminal: bool
    allowed_next: tuple[str, ...]
    hypothesis_count: int = 0
    blocked_reason: str | None = None
    workflow: str = "legacy"
    workflow_phases: tuple[WorkflowPhase, ...] = ()
    current_phase_id: str | None = None
    approval_evidence_refs: tuple[str, ...] = ()

    @property
    def pending_intent(self) -> PendingIntent | None:
        return self.unresolved_intents[0] if self.unresolved_intents else None

    @property
    def latest_event_hash(self) -> str:
        return self.event_hashes[-1]

    @property
    def pending_operation_id(self) -> str | None:
        intent = self.pending_intent
        return intent.operation_id if intent is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "contract_id": self.contract_id,
            "contract_hash": self.contract_hash,
            "state": self.state.value,
            "phase": self.phase,
            "approval_required": bool(self.contract_snapshot.get("approval_required")),
            "requirement_results": _thaw(self.requirement_results),
            "hypothesis_count": self.hypothesis_count,
            "reflection_done": self.reflection is not None,
            "pending_operation_id": self.pending_operation_id,
            "latest_event_hash": self.latest_event_hash,
            "terminal": self.terminal,
            "allowed_next": list(self.allowed_next),
            "workflow": self.workflow,
            "workflow_phases": [phase.to_dict() for phase in self.workflow_phases],
            "current_phase_id": self.current_phase_id,
            "blocked_reason": self.blocked_reason,
            "approval_evidence_refs": list(self.approval_evidence_refs),
        }


@dataclass(frozen=True)
class TaskActionReceipt:
    session: TaskSession
    response: Mapping[str, Any]
    ephemeral: Mapping[str, str] = field(default_factory=dict)
    replayed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "response", _freeze(self.response))
        object.__setattr__(self, "ephemeral", _freeze(self.ephemeral))


def _error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    **details: Any,
) -> TaskLifecycleError:
    return TaskLifecycleError(code, message, retryable, details)


def _validate_phase_evidence_events(
    events: list[LedgerEvent],
    phase: WorkflowPhase,
    refs: tuple[str, ...],
    *,
    start_index: int,
    status: str,
) -> tuple[str, ...]:
    if not refs:
        raise _error(
            "phase_evidence_incomplete",
            "phase finish requires task-scoped evidence",
        )
    if any(not isinstance(ref, str) or not _SHA256.fullmatch(ref) for ref in refs):
        raise _error(
            "phase_evidence_incomplete",
            "phase evidence references must be SHA-256 hashes",
        )
    if len(refs) != len(set(refs)):
        raise _error(
            "phase_evidence_incomplete",
            "phase evidence references must be unique",
        )
    indexed = {event.entry_hash: (index, event) for index, event in enumerate(events)}
    if any(ref not in indexed for ref in refs):
        raise _error(
            "evidence_scope_mismatch",
            "phase evidence reference is not in this task",
        )

    action = False
    work_evidence = False
    positive_evidence_refs: dict[str, int] = {}
    verification_evidence_refs: set[str] = set()
    latest_mutation_index = max(
        (
            index
            for index, event in enumerate(events)
            if index > start_index and event.payload.get("mutates_task") is True
        ),
        default=-1,
    )
    verdicts: list[tuple[str, tuple[str, ...], int]] = []
    for ref in refs:
        index, event = indexed[ref]
        if index <= start_index:
            raise _error(
                "phase_evidence_stale",
                "phase evidence must follow the current phase start",
                evidence_ref=ref,
            )
        positive = True
        recognized = True
        if event.event_type == TASK_ACTION_RESULT:
            completed = event.payload.get("outcome") == "completed"
            action_completed = completed and event.payload.get("operation") == "action"
            action = action or action_completed
            work_evidence = work_evidence or action_completed
            positive = completed
        elif event.event_type == AuditEventType.EVIDENCE.value:
            evidence_status = event.payload.get("status")
            positive = evidence_status not in {"fail", "blocked", "timeout", "error"}
            if positive:
                positive_evidence_refs[ref] = index
                if (
                    event.payload.get("kind")
                    == EvidenceKind.VERIFICATION_RESULT.value
                    and evidence_status == "pass"
                ):
                    verification_evidence_refs.add(ref)
            work_evidence = work_evidence or positive
        elif event.event_type == AuditEventType.VERIFIER_DECISION.value:
            positive = event.payload.get("status") == "pass"
            verifier = event.payload.get("verifier")
            citations = event.payload.get("evidence_refs")
            if (
                positive
                and isinstance(verifier, str)
                and verifier.strip()
                and isinstance(citations, (list, tuple))
            ):
                verdicts.append(
                    (verifier.strip().casefold(), tuple(citations), index)
                )
        elif event.event_type == TASK_OPERATION:
            response = event.payload.get("response")
            if event.payload.get("operation") == "hypothesis" and isinstance(
                response, Mapping
            ):
                positive = response.get("status") == "supported"
                work_evidence = work_evidence or positive
            elif event.payload.get("operation") == "verify" and isinstance(
                response, Mapping
            ):
                request = event.payload.get("request")
                evidence_ref = response.get("evidence_hash")
                decision_ref = response.get("decision_hash")
                evidence_record = (
                    indexed.get(evidence_ref) if isinstance(evidence_ref, str) else None
                )
                decision_record = (
                    indexed.get(decision_ref) if isinstance(decision_ref, str) else None
                )
                positive = bool(
                    isinstance(request, Mapping)
                    and request.get("mode") == "manual"
                    and response.get("status") == "pass"
                    and evidence_record is not None
                    and evidence_record[0] > latest_mutation_index
                    and decision_record is not None
                    and decision_record[1].event_type
                    == AuditEventType.HUMAN_DECISION.value
                    and decision_record[1].payload.get("approved") is True
                    and decision_record[1].payload.get("evidence_hash") == evidence_ref
                )
                if positive:
                    positive_evidence_refs[ref] = index
                    verification_evidence_refs.add(ref)
                    verification_evidence_refs.add(evidence_ref)
                work_evidence = work_evidence or positive
            else:
                recognized = False
        else:
            recognized = False
        if not recognized:
            raise _error(
                "phase_evidence_incomplete",
                "phase evidence type is not supported",
                evidence_ref=ref,
            )
        if status == "passed" and not positive:
            raise _error(
                "phase_evidence_incomplete",
                "passed phase cites non-passing or unsupported evidence",
                evidence_ref=ref,
            )

    cited_refs = set(refs)
    fresh_evidence_refs = {
        ref
        for ref, index in positive_evidence_refs.items()
        if index > latest_mutation_index
    }
    fresh_verification_refs = {
        ref
        for ref in fresh_evidence_refs
        if ref in verification_evidence_refs
    }
    verdict_evidence_refs = (
        fresh_verification_refs if phase.requires_verification else fresh_evidence_refs
    )
    passing_verifiers = {
        verifier
        for verifier, citations, verdict_index in verdicts
        if citations
        and set(citations) <= cited_refs
        and any(
            verdict_index > positive_evidence_refs[ref]
            for ref in set(citations) & verdict_evidence_refs
        )
    }
    verification = bool(fresh_verification_refs)
    if status == "passed" and (
        not work_evidence
        or (phase.requires_action and not action)
        or (phase.requires_verification and not verification)
        or len(passing_verifiers) < phase.requires_verdicts
    ):
        raise _error(
            "phase_evidence_incomplete",
            "phase evidence does not satisfy its action, verification, and verdict requirements",
            requires_action=phase.requires_action,
            requires_verification=phase.requires_verification,
            requires_verdicts=phase.requires_verdicts,
            passing_verdicts=len(passing_verifiers),
        )
    return refs


def _required_text(payload: Mapping[str, Any], name: str, event: LedgerEvent) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise _error(
            "invalid_task_event",
            f"{event.event_type}.{name} must be a non-blank string",
            event_hash=event.entry_hash,
            field=name,
        )
    return value.strip()


def _request_digest(payload: Mapping[str, Any], event: LedgerEvent) -> str:
    value = _required_text(payload, "request_sha256", event).lower()
    if not _SHA256.fullmatch(value):
        raise _error(
            "invalid_task_event",
            f"{event.event_type}.request_sha256 must be a lowercase SHA-256",
            event_hash=event.entry_hash,
        )
    return value


def _common_payload(event: LedgerEvent, task_id: str) -> Mapping[str, Any]:
    payload = event.payload
    if not isinstance(payload, Mapping):
        raise _error(
            "invalid_task_event",
            f"{event.event_type} payload must be an object",
            event_hash=event.entry_hash,
        )
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise _error(
            "unsupported_task_schema",
            f"{event.event_type} requires schema_version 1",
            event_hash=event.entry_hash,
            schema_version=payload.get("schema_version"),
        )
    if payload.get("task_id") != task_id or event.contract_id != task_id:
        raise _error(
            "task_identity_mismatch",
            "task event identity does not match its contract scope",
            event_hash=event.entry_hash,
            task_id=task_id,
            payload_task_id=payload.get("task_id"),
            contract_id=event.contract_id,
        )
    return payload


def _response(payload: Mapping[str, Any], event: LedgerEvent) -> Any:
    if "response" not in payload:
        raise _error(
            "invalid_task_event",
            f"{event.event_type}.response is required",
            event_hash=event.entry_hash,
        )
    return _freeze(payload["response"])


def _outcome(payload: Mapping[str, Any], event: LedgerEvent) -> str:
    value = payload.get("outcome")
    if not isinstance(value, str) or not value.strip():
        raise _error(
            "invalid_task_event",
            f"{event.event_type}.outcome must be a non-blank string",
            event_hash=event.entry_hash,
        )
    return value.strip()


def _state(value: Any, event: LedgerEvent, field_name: str) -> TaskState:
    try:
        return TaskState(value)
    except (TypeError, ValueError) as exc:
        raise _error(
            "invalid_task_transition",
            f"{field_name} is not a task state",
            event_hash=event.entry_hash,
            value=value,
        ) from exc


def _is_correlated_approval_rejection(event: LedgerEvent, task_id: str) -> bool:
    payload = event.payload
    return bool(
        event.event_type == AuditEventType.HUMAN_DECISION.value
        and isinstance(payload, Mapping)
        and payload.get("task_id") == task_id
        and payload.get("approved") is False
        and payload.get("stage") in {"plan", "final"}
        and payload.get("operation") in {None, "approve"}
        and all(
            isinstance(payload.get(name), str) and bool(payload[name].strip())
            for name in (
                "operation_id",
                "idempotency_key",
                "request_sha256",
            )
        )
    )


def _phase(
    state: TaskState,
    unresolved: tuple[PendingIntent, ...],
    reflection: Mapping[str, Any] | None,
) -> str:
    if any(intent.kind == "action" for intent in unresolved):
        return "recovery"
    if state is TaskState.BLOCKED:
        return "recovery"
    if state is TaskState.PLANNED:
        return "plan"
    if state is TaskState.APPROVED:
        return "approval"
    if state is TaskState.EXECUTING:
        return "execution"
    if reflection is None:
        return "reflection"
    return "done"


def _allowed_next(
    state: TaskState,
    unresolved: tuple[PendingIntent, ...],
    reflection: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    if any(intent.kind == "action" for intent in unresolved):
        return ("resolve",)
    if any(intent.kind == "completion" for intent in unresolved):
        return ("complete",)
    if state is TaskState.PLANNED:
        return ("approve", "action", "reject")
    if state is TaskState.APPROVED:
        return ("action", "verify", "reject")
    if state is TaskState.EXECUTING:
        return ("action", "verify", "verdict", "complete")
    if state is TaskState.BLOCKED:
        return ("approve",)
    if reflection is None:
        return ("reflect",)
    return ()


def _workflow_allowed_next(
    state: TaskState,
    unresolved: tuple[PendingIntent, ...],
    reflection: Mapping[str, Any] | None,
    phases: tuple[WorkflowPhase, ...],
    current: WorkflowPhase | None,
) -> tuple[str, ...]:
    if not phases or unresolved or state in {TaskState.VERIFIED, TaskState.REJECTED}:
        return _allowed_next(state, unresolved, reflection)
    if state is TaskState.BLOCKED or (current and current.status == "blocked"):
        return ("approve",)
    if current is None:
        return ("verify", "verdict", "append_evidence", "complete")
    if current.status == "running":
        operations = [
            "action",
            "verify",
            "verdict",
            "append_evidence",
        ]
        if (current.playbook, current.name) in {
            ("root-cause-protocol", "hypothesis"),
            ("debugging", "isolate"),
        }:
            operations.append("hypothesis")
        operations.extend(("phase_finish", "reject"))
        return tuple(operations)
    if state is TaskState.PLANNED:
        return ("approve", "phase_start", "reject")
    return ("phase_start", "reject")


class TaskLifecycle:
    """Write task events and reconstruct immutable state from the same ledger."""

    def __init__(
        self,
        project_root: str | Path,
        ledger_path: str | Path | None = None,
        *,
        policy: TaskPolicy | None = None,
        approval_authorizer: ApprovalAuthorizer | None = None,
        effect_runner: EffectRunner | None = None,
        http_adapter: HttpAdapter | None = None,
        http_credentials: Mapping[str, Mapping[str, str]] | None = None,
        browser_adapter: A11yBrowserAdapter | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.ledger = EvidenceLedger(
            Path(ledger_path).resolve()
            if ledger_path is not None
            else self.project_root / ".causality" / "ledger.jsonl"
        )
        self.runtime = Causality(self.ledger.path, project_root=self.project_root)
        self.policy = policy or TaskPolicy()
        # The public lifecycle is a security boundary too, not merely the MCP
        # wrapper's implementation detail.  Absence of a trust provider must
        # therefore deny approvals and recovery instead of silently trusting
        # every caller.
        self.approval_authorizer = approval_authorizer or (
            lambda _approver, _stage, _proof: False
        )
        self.effect_runner = effect_runner
        self.browser_adapter = browser_adapter
        enabled_browser_tools = self.policy.allowed_tools & _BROWSER_TOOLS
        if enabled_browser_tools and self.browser_adapter is None:
            raise ValueError("browser tools require an explicitly configured driver")
        if self.browser_adapter is not None:
            if self.browser_adapter.timeout_seconds > self.policy.max_timeout_seconds:
                raise ValueError("browser adapter timeout may not exceed TaskPolicy")
            if enabled_browser_tools:
                self.browser_adapter.capabilities()
        self.http_adapter = http_adapter or HttpAdapter(
            max_request_bytes=self.policy.max_http_request_bytes,
            max_response_bytes=self.policy.max_http_response_bytes,
        )
        if (
            self.http_adapter.max_request_bytes > self.policy.max_http_request_bytes
            or self.http_adapter.max_response_bytes > self.policy.max_http_response_bytes
        ):
            raise ValueError("http_adapter byte limits may not exceed TaskPolicy")
        credentials: dict[str, Mapping[str, str]] = {}
        for ref, headers in (http_credentials or {}).items():
            if (
                not isinstance(ref, str)
                or not _IDEMPOTENCY_KEY.fullmatch(ref)
                or not isinstance(headers, Mapping)
                or not headers
            ):
                raise ValueError("http_credentials must map aliases to non-empty headers")
            try:
                validated = HttpRequest(
                    "GET",
                    "https://credential-validation.invalid",
                    headers=headers,
                ).headers
            except (TypeError, ValueError) as exc:
                raise ValueError("http credential headers are invalid") from exc
            credentials[ref] = validated
        self.http_credentials = MappingProxyType(credentials)

    def session(self, task_id: str, recover: bool = True) -> TaskSession:
        return self.get(task_id, recover=recover)

    def _effective_contract(self, contract: GoalContract, task_id: str) -> GoalContract:
        requested = set(contract.permissions.allowed_tools)
        denied = requested - self.policy.allowed_tools
        if denied:
            raise _error(
                "policy_denied",
                "task requests tools outside server policy",
                tools=sorted(denied),
            )
        try:
            network_scope = tuple(
                dict.fromkeys(
                    normalize_origin(origin, scope=True)
                    for origin in contract.permissions.network_scope
                )
            )
        except (TypeError, ValueError) as exc:
            raise _error(
                "validation_error",
                "network_scope must contain exact HTTP origins",
            ) from exc
        denied_origins = set(network_scope) - self.policy.allowed_network_origins
        if denied_origins:
            raise _error(
                "policy_denied",
                "task requests network origins outside server policy",
                origins=sorted(denied_origins),
            )
        auth_scope = tuple(dict.fromkeys(contract.permissions.auth_scope))
        if any(
            not isinstance(ref, str) or not _IDEMPOTENCY_KEY.fullmatch(ref)
            for ref in auth_scope
        ):
            raise _error(
                "validation_error",
                "auth_scope must contain non-blank credential aliases",
            )
        denied_auth = set(auth_scope) - self.policy.allowed_auth_refs
        if denied_auth:
            raise _error(
                "policy_denied",
                "task requests credential aliases outside server policy",
                auth_refs=sorted(denied_auth),
            )
        for entry in contract.permissions.write_scope:
            candidate = Path(entry)
            if not candidate.is_absolute():
                candidate = self.project_root / candidate
            if not _within(candidate.resolve(), self.project_root):
                raise _error(
                    "scope_escape",
                    "write_scope escapes the project root",
                    path=str(entry),
                )
        undeclared_commands = [
            list(requirement.argv)
            for requirement in contract.verification_requirements
            if not requirement.manual
            and not self.policy.allows_verification(requirement.argv)
        ]
        if undeclared_commands:
            raise _error(
                "policy_denied",
                "verification argv is not in the server-owned exact allowlist",
                commands=undeclared_commands,
            )
        excessive_timeouts = [
            requirement.id
            for requirement in contract.verification_requirements
            if not requirement.manual
            and requirement.timeout_seconds > self.policy.max_timeout_seconds
        ]
        if excessive_timeouts:
            raise _error(
                "policy_denied",
                "verification timeout exceeds the server policy ceiling",
                requirements=excessive_timeouts,
                max_timeout_seconds=self.policy.max_timeout_seconds,
            )
        return replace(
            contract,
            goal_id=task_id,
            state=StateTransition.PLANNED,
            workspace_root="",
            permissions=PermissionContract(
                allowed_tools=tuple(contract.permissions.allowed_tools),
                write_scope=tuple(contract.permissions.write_scope),
                network_scope=network_scope,
                auth_scope=auth_scope,
            ),
        )

    def begin(
        self,
        contract: GoalContract,
        *,
        idempotency_key: str,
        workflow: str = "legacy",
    ) -> TaskSession:
        key = _idempotency_key(idempotency_key)
        if not isinstance(contract, GoalContract):
            raise _error("invalid_request", "begin requires a GoalContract")
        if not isinstance(workflow, str):
            raise _error("validation_error", "workflow must be text")
        workflow = workflow.strip()
        if workflow not in {"legacy", "auto", "root-cause-protocol"}:
            raise _error(
                "validation_error",
                "workflow must be legacy, auto, or root-cause-protocol",
            )
        task_id = str(
            uuid5(
                NAMESPACE_URL,
                f"causality:{str(self.project_root).casefold()}:{key}",
            )
        )
        with self.runtime.execution_lock():
            if not self.ledger.verify_chain():
                raise _error("ledger_integrity_failed", "ledger hash chain verification failed")
            for event in self.ledger.events(all_segments=True):
                if event.event_type != TASK_STARTED or event.payload.get("idempotency_key") != key:
                    continue
                if not event.contract_id:
                    raise _error("invalid_task_event", "task_started has no contract scope")
                session = self.get(event.contract_id)
                frozen_plan = tuple(event.payload.get("phase_plan", ()))
                request = (
                    _contract_request(contract)
                    if workflow == "legacy"
                    else _workflow_begin_request(contract, workflow, frozen_plan)
                )
                digest = canonical_sha256(request)
                prior = event.payload.get("request_sha256")
                if prior != digest:
                    raise _error(
                        "idempotency_conflict",
                        "begin idempotency key is already bound to another request",
                        idempotency_key=key,
                    )
                return session

            phase_plan = _workflow_snapshot(contract, workflow)
            request = (
                _contract_request(contract)
                if workflow == "legacy"
                else _workflow_begin_request(contract, workflow, phase_plan)
            )
            digest = canonical_sha256(request)

            snapshot = self.ledger.contract_snapshot(task_id)
            if snapshot is not None:
                existing_request = _contract_request(
                    GoalContract.from_mapping(snapshot)
                )
                if workflow != "legacy":
                    existing_request = _workflow_begin_request(
                        GoalContract.from_mapping(snapshot),
                        workflow,
                        phase_plan,
                    )
                if canonical_sha256(existing_request) != digest:
                    raise _error(
                        "idempotency_conflict",
                        "deterministic task identity is bound to another request",
                        idempotency_key=key,
                    )
                bound = GoalContract.from_mapping(snapshot)
            else:
                bound = self.runtime.create_contract(
                    self._effective_contract(contract, task_id)
                )
            self.ledger.append(
                AuditEventType.TASK_STARTED,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "idempotency_key": key,
                    "request_sha256": digest,
                    "request": request,
                    "workflow": workflow,
                    "phase_plan": list(phase_plan),
                    "response": {
                        "task_id": task_id,
                        "contract_id": task_id,
                        "workflow": workflow,
                        "phase_plan": list(phase_plan),
                    },
                },
                contract_id=bound.goal_id,
            )
            return self.get(task_id)

    @staticmethod
    def _operation_id(task_id: str, operation: str, key: str, digest: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"{task_id}:{operation}:{key}:{digest}"))

    def _operation_metadata(
        self,
        task_id: str,
        operation: str,
        key: str,
        digest: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": task_id,
            "operation": operation,
            "operation_id": self._operation_id(task_id, operation, key, digest),
            "idempotency_key": key,
            "request_sha256": digest,
        }

    def _authoritative_event(
        self,
        task_id: str,
        operation: str,
        key: str,
        digest: str,
        event_type: AuditEventType,
    ) -> LedgerEvent | None:
        matches = []
        for event in self.ledger.events_for_contract(task_id, all_segments=True):
            payload = event.payload
            if (
                event.event_type == event_type.value
                and payload.get("operation") == operation
                and payload.get("idempotency_key") == key
            ):
                if payload.get("request_sha256") != digest:
                    raise _error(
                        "idempotency_conflict",
                        "idempotency key is bound to another authoritative event",
                        operation=operation,
                        idempotency_key=key,
                    )
                matches.append(event)
        if len(matches) > 1:
            raise _error(
                "invalid_task_event",
                "duplicate authoritative events share one idempotency key",
                operation=operation,
                idempotency_key=key,
            )
        return matches[0] if matches else None

    def _assert_completion_snapshot_current(
        self,
        task_id: str,
        gate_event_hash: str,
        *,
        operation_event_hash: str | None = None,
    ) -> None:
        """Reject a completion decision if task history advanced after it.

        A process may die after the gate append releases its lock but before
        the terminal transition is durable.  Reusing that PASS after any other
        task event would certify a state the gate never examined.
        """

        events = self.ledger.events_for_contract(task_id, all_segments=True)
        positions = {event.entry_hash: index for index, event in enumerate(events)}
        gate_index = positions.get(gate_event_hash)
        if gate_index is None:
            raise _error(
                "invalid_task_event",
                "completion operation cites a missing gate decision",
                gate_event_hash=gate_event_hash,
            )
        gate_event = events[gate_index]
        if (
            gate_event.event_type != AuditEventType.GATE_DECISION.value
            or gate_event.payload.get("operation") != "complete"
        ):
            raise _error(
                "invalid_task_event",
                "completion cites a non-completion gate event",
                gate_event_hash=gate_event_hash,
            )
        expected_fingerprint = gate_event.payload.get(
            "completion_workspace_fingerprint_sha256"
        )
        current_fingerprint = self._workspace_fingerprint_digest()
        if gate_event.payload.get("decision") == GateDecision.PASS.value and (
            not isinstance(expected_fingerprint, str)
            or not _SHA256.fullmatch(expected_fingerprint)
            or expected_fingerprint != current_fingerprint
        ):
            raise _error(
                "completion_snapshot_stale",
                "workspace differs from the completion decision; use a new "
                "idempotency key after current verification",
                retryable=False,
                gate_event_hash=gate_event_hash,
                expected_workspace_fingerprint_sha256=expected_fingerprint,
                actual_workspace_fingerprint_sha256=current_fingerprint,
            )

        allowed_hashes: set[str] = set()
        if operation_event_hash is not None:
            operation_index = positions.get(operation_event_hash)
            operation_event = (
                events[operation_index] if operation_index is not None else None
            )
            operation_response = (
                operation_event.payload.get("response")
                if operation_event is not None
                and isinstance(operation_event.payload, Mapping)
                else None
            )
            if (
                operation_event is None
                or operation_index <= gate_index
                or operation_event.event_type != TASK_OPERATION
                or operation_event.payload.get("operation") != "complete"
                or not isinstance(operation_response, Mapping)
                or operation_response.get("gate_event_hash") != gate_event_hash
            ):
                raise _error(
                    "invalid_task_event",
                    "completion operation does not match its gate decision",
                    gate_event_hash=gate_event_hash,
                    operation_event_hash=operation_event_hash,
                )
            allowed_hashes.add(operation_event_hash)

        invalidating = next(
            (
                event
                for event in events[gate_index + 1 :]
                if event.entry_hash not in allowed_hashes
            ),
            None,
        )
        if invalidating is not None:
            raise _error(
                "completion_snapshot_stale",
                "task history advanced after the completion decision; use a new "
                "idempotency key after current verification",
                retryable=False,
                gate_event_hash=gate_event_hash,
                invalidating_event_hash=invalidating.entry_hash,
                invalidating_event_type=invalidating.event_type,
            )

    def _workspace_fingerprint_digest(self) -> str:
        from .verification import workspace_fingerprint, workspace_fingerprint_digest

        return workspace_fingerprint_digest(
            workspace_fingerprint(self.project_root, self.ledger.path)
        )

    @staticmethod
    def _existing(
        session: TaskSession,
        operation: str,
        key: str,
        digest: str,
    ) -> IdempotencyRecord | None:
        record = session.idempotency.get((operation, key))
        if record is None:
            return None
        if record.request_sha256 != digest:
            raise _error(
                "idempotency_conflict",
                "idempotency key is bound to another request",
                operation=operation,
                idempotency_key=key,
            )
        if not record.complete:
            raise _error(
                "unresolved_action_intent",
                "operation has a durable intent without a result",
                operation_id=record.operation_id,
            )
        return record

    def _append_transition(
        self,
        session: TaskSession,
        target: TaskState,
        *,
        reason: str,
        cause_event_hash: str,
    ) -> LedgerEvent:
        if target not in _TRANSITIONS[session.state]:
            raise _error(
                "invalid_transition",
                f"illegal task state edge {session.state.value}->{target.value}",
            )
        return self.ledger.append(
            AuditEventType.STATE_TRANSITION,
            {
                "schema_version": SCHEMA_VERSION,
                "task_id": session.task_id,
                "from_state": session.state.value,
                "state": target.value,
                "reason": reason,
                "cause_event_hash": cause_event_hash,
            },
            contract_id=session.task_id,
        )

    def _append_operation(
        self,
        session: TaskSession,
        operation: str,
        key: str,
        digest: str,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
    ) -> LedgerEvent:
        return self.ledger.append(
            AuditEventType.TASK_OPERATION,
            {
                "schema_version": SCHEMA_VERSION,
                "task_id": session.task_id,
                "operation": operation,
                "operation_id": self._operation_id(session.task_id, operation, key, digest),
                "idempotency_key": key,
                "request_sha256": digest,
                "request": dict(request),
                "response": dict(response),
                "outcome": "completed",
            },
            contract_id=session.task_id,
        )

    def _contract(self, session: TaskSession) -> GoalContract:
        return GoalContract.from_mapping(_thaw(session.contract_snapshot))

    def _ensure_executing(self, session: TaskSession) -> TaskSession:
        if session.terminal:
            raise _error("task_terminal", "terminal task cannot execute new work")
        if session.state is TaskState.BLOCKED:
            raise _error("task_blocked", "blocked task requires trusted resolution")
        if session.state is TaskState.PLANNED:
            gate = self.runtime.evaluate_plan(self._contract(session))
            cause = self.ledger.latest_hash_for_contract(session.task_id)
            if not gate.allowed or cause is None:
                raise _error(
                    "approval_required",
                    gate.reasons[0] if gate.reasons else "plan approval required",
                )
            self._append_transition(
                session,
                TaskState.APPROVED,
                reason="plan gate passed",
                cause_event_hash=cause,
            )
            session = self.get(session.task_id)
        if session.state is TaskState.APPROVED:
            self._append_transition(
                session,
                TaskState.EXECUTING,
                reason="task operation started",
                cause_event_hash=session.latest_event_hash,
            )
            session = self.get(session.task_id)
        if session.state is not TaskState.EXECUTING:
            raise _error("invalid_transition", "task is not executable")
        return session

    def _resolve_project_path(self, value: str, *, field_name: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise _error("validation_error", f"{field_name} must be non-blank")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        resolved = candidate.resolve()
        if not _within(resolved, self.project_root):
            raise _error("scope_escape", f"{field_name} escapes the project root")
        return resolved

    @staticmethod
    def _subprocess_action_kind(argv: list[str]) -> str:
        executable = Path(argv[0]).name.casefold()
        lowered = [item.casefold() for item in argv[1:]]
        if executable in {"rm", "rmdir", "del", "erase"}:
            return "delete"
        if executable in {"git", "git.exe"} and "push" in lowered:
            return "external_send"
        if any(item in {"deploy", "publish", "release"} for item in lowered):
            return "deploy"
        return "tool_call"

    def _scoped_http_path(
        self,
        contract: GoalContract,
        value: str,
        *,
        field_name: str,
    ) -> Path:
        path = self._resolve_project_path(value, field_name=field_name)
        scopes = tuple(contract.permissions.write_scope)
        if not scopes:
            raise _error("action_blocked", "empty write_scope grants no HTTP file access")
        resolved_scopes = tuple(
            self._resolve_project_path(entry, field_name="write_scope")
            for entry in scopes
        )
        if not any(_within(path, scope) for scope in resolved_scopes):
            raise _error("action_blocked", f"{field_name} is outside write_scope")
        return path

    def _normalize_http_action(
        self,
        contract: GoalContract,
        action: Mapping[str, Any],
    ) -> _ActionPlan:
        allowed_fields = {
            "kind",
            "method",
            "url",
            "headers",
            "body_ref",
            "timeout_seconds",
            "expected_statuses",
            "response_artifact",
            "auth_ref",
        }
        if any(not isinstance(name, str) for name in action):
            raise _error("validation_error", "HTTP action field names must be text")
        unknown = set(action) - allowed_fields
        if unknown:
            raise _error(
                "validation_error",
                "HTTP action contains unknown fields",
                fields=sorted(str(item) for item in unknown),
            )
        method = action.get("method")
        if not isinstance(method, str) or method.upper() not in _HTTP_METHODS:
            raise _error("validation_error", "HTTP method is not supported")
        method = method.upper()
        url = action.get("url")
        if not isinstance(url, str):
            raise _error("validation_error", "HTTP url must be text")
        try:
            origin = normalize_origin(url)
        except ValueError as exc:
            raise _error("validation_error", "HTTP url is invalid") from exc

        raw_headers = action.get("headers", {})
        if not isinstance(raw_headers, Mapping):
            raise _error("validation_error", "HTTP headers must be an object")
        headers = dict(raw_headers)
        if any(not isinstance(name, str) for name in headers):
            raise _error("validation_error", "HTTP header names must be text")
        forbidden = sorted(
            name for name in headers if name.casefold() in _CALLER_FORBIDDEN_HTTP_HEADERS
        )
        if forbidden:
            raise _error(
                "validation_error",
                "authority and credential headers must be server-owned",
                headers=forbidden,
            )
        denied_headers = {
            name.casefold() for name in headers
        } - self.policy.allowed_http_headers
        if denied_headers:
            raise _error(
                "policy_denied",
                "caller HTTP headers are outside server policy",
                headers=sorted(denied_headers),
            )

        timeout = action.get("timeout_seconds", 30.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise _error("validation_error", "HTTP timeout must be numeric")
        timeout = float(timeout)
        if timeout <= 0 or timeout > self.policy.max_timeout_seconds:
            raise _error("validation_error", "HTTP timeout is outside server policy")

        expected = action.get("expected_statuses")
        if (
            not isinstance(expected, (list, tuple))
            or not expected
            or any(type(status) is not int or not 100 <= status <= 599 for status in expected)
        ):
            raise _error(
                "validation_error",
                "expected_statuses must contain HTTP status integers",
            )
        expected_statuses = tuple(dict.fromkeys(expected))

        body_ref = action.get("body_ref")
        body_path: Path | None = None
        body: bytes | None = None
        if body_ref is not None:
            body_path = self._scoped_http_path(
                contract,
                body_ref,
                field_name="body_ref",
            )
            if not body_path.is_file():
                raise _error("validation_error", "body_ref must name an existing file")
            try:
                with body_path.open("rb") as stream:
                    body = stream.read(self.policy.max_http_request_bytes + 1)
            except OSError as exc:
                raise _error("validation_error", "body_ref could not be read") from exc
            if len(body) > self.policy.max_http_request_bytes:
                raise _error("validation_error", "HTTP request body exceeds server policy")

        artifact_ref = action.get("response_artifact")
        artifact: Path | None = None
        if artifact_ref is not None:
            artifact = self._scoped_http_path(
                contract,
                artifact_ref,
                field_name="response_artifact",
            )
            if not artifact.parent.is_dir():
                raise _error(
                    "validation_error",
                    "response_artifact parent must already exist",
                )
            if artifact.exists() and not artifact.is_file():
                raise _error(
                    "validation_error",
                    "response_artifact must name a file",
                )

        auth_ref = action.get("auth_ref")
        if auth_ref is not None and (
            not isinstance(auth_ref, str) or not _IDEMPOTENCY_KEY.fullmatch(auth_ref)
        ):
            raise _error("validation_error", "auth_ref must be a credential alias")
        try:
            request = HttpRequest(
                method,
                url,
                headers=headers,
                body=body,
                timeout=timeout,
                artifact_path=artifact,
            )
        except (TypeError, ValueError) as exc:
            raise _error("validation_error", "HTTP request is invalid") from exc
        descriptor = {
            "kind": "http",
            "method": method,
            "origin": origin,
            "url_sha256": sha256_text(url),
            "header_names": sorted(name.casefold() for name in headers),
            "headers_sha256": canonical_sha256(headers),
            "body_ref": str(body_path) if body_path is not None else None,
            "body_bytes": len(body or b""),
            "body_sha256": hashlib.sha256(body or b"").hexdigest(),
            "timeout_seconds": timeout,
            "expected_statuses": list(expected_statuses),
            "response_artifact": str(artifact) if artifact is not None else None,
            "auth_ref": auth_ref,
        }
        return _ActionPlan(
            descriptor=descriptor,
            tool="http",
            action_kind="external_send",
            description=f"{method} request to {origin}",
            network_origin=origin,
            auth_ref=auth_ref,
            http_request=request,
            expected_statuses=expected_statuses,
        )

    @staticmethod
    def _browser_ref(value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not REF_RE.fullmatch(value):
            raise _error(
                "validation_error",
                f"{field_name} must be a stable browser ref (@eN or @cN)",
            )
        return value

    @staticmethod
    def _browser_state_hash(value: Any) -> str:
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise _error(
                "validation_error",
                "expected_state_hash must be a lowercase SHA-256",
            )
        return value

    def _normalize_browser_action(self, action: Mapping[str, Any]) -> _ActionPlan:
        if any(not isinstance(name, str) for name in action):
            raise _error("validation_error", "browser action field names must be text")
        operation = action.get("operation")
        if operation not in _BROWSER_TOOL_BY_OPERATION:
            raise _error("validation_error", "browser operation is not supported")
        common = {"kind", "operation"}
        fields = {
            "observe": (common | {"mode", "scope", "annotate"}, common),
            "act": (
                common | {"action", "ref", "value", "expected_state_hash"},
                common | {"action", "ref", "expected_state_hash"},
            ),
            "assert": (
                common | {"property", "ref", "expected_state_hash"},
                common | {"property", "ref", "expected_state_hash"},
            ),
            "inspect": (
                common | {"inspection", "ref", "expected_state_hash"},
                common | {"inspection", "ref", "expected_state_hash"},
            ),
            "visual": (
                common | {"ref", "expected_state_hash"},
                common | {"expected_state_hash"},
            ),
        }
        allowed, required = fields[operation]
        unknown = set(action) - allowed
        missing = required - set(action)
        if unknown or missing:
            raise _error(
                "validation_error",
                "browser action fields do not match the operation",
                unknown=sorted(unknown),
                missing=sorted(missing),
            )

        mode = action.get("mode", "interactive")
        scope = action.get("scope")
        annotate = action.get("annotate", False)
        browser_action = action.get("action")
        ref = action.get("ref")
        value = action.get("value")
        expected = action.get("expected_state_hash")
        assertion = action.get("property")
        inspection = action.get("inspection")
        if operation == "observe":
            if mode not in _BROWSER_MODES:
                raise _error("validation_error", "browser observe mode is invalid")
            if scope is not None:
                scope = self._browser_ref(scope, "scope")
            if not isinstance(annotate, bool):
                raise _error("validation_error", "browser annotate must be a boolean")
        else:
            expected = self._browser_state_hash(expected)
            if operation in {"act", "assert", "inspect"}:
                ref = self._browser_ref(ref, "ref")
            elif ref is not None:
                ref = self._browser_ref(ref, "ref")
        if operation == "act":
            if browser_action not in {"click", "fill", "hover", "press", "select"}:
                raise _error("validation_error", "browser action type is invalid")
            if browser_action in {"fill", "press", "select"} and not isinstance(
                value, str
            ):
                raise _error(
                    "validation_error", f"browser {browser_action} requires text value"
                )
            if browser_action in {"click", "hover"} and value is not None:
                raise _error(
                    "validation_error", f"browser {browser_action} rejects value"
                )
            if (
                isinstance(value, str)
                and self.browser_adapter is not None
                and len(value.encode("utf-8"))
                > self.browser_adapter.max_action_value_bytes
            ):
                raise _error(
                    "validation_error",
                    "browser action value exceeds the server limit",
                )
        if operation == "assert" and assertion not in ASSERTION_PROPERTIES:
            raise _error("validation_error", "browser assertion property is invalid")
        if operation == "inspect" and inspection not in INSPECTION_KINDS:
            raise _error("validation_error", "browser inspection kind is invalid")

        browser = _BrowserPlan(
            operation=operation,
            mode=mode,
            scope=scope,
            annotate=annotate,
            action=browser_action,
            ref=ref,
            value=value,
            expected_state_hash=expected,
            assertion=assertion,
            inspection=inspection,
        )
        descriptor: dict[str, Any] = {
            "kind": "browser",
            "operation": operation,
        }
        if operation == "observe":
            descriptor.update({"mode": mode, "scope": scope, "annotate": annotate})
        else:
            descriptor.update({"ref": ref, "expected_state_hash": expected})
        if operation == "act":
            encoded = (value or "").encode("utf-8")
            descriptor.update(
                {
                    "action": browser_action,
                    "value_bytes": len(encoded),
                    "value_sha256": hashlib.sha256(encoded).hexdigest(),
                }
            )
        elif operation == "assert":
            descriptor["property"] = assertion
        elif operation == "inspect":
            descriptor["inspection"] = inspection
        tool = _BROWSER_TOOL_BY_OPERATION[operation]
        return _ActionPlan(
            descriptor=descriptor,
            tool=tool,
            action_kind="external_send" if operation == "act" else "tool_call",
            description=f"browser {operation}",
            browser=browser,
        )

    def _normalize_action(
        self,
        session: TaskSession,
        action: Mapping[str, Any],
    ) -> _ActionPlan:
        if not isinstance(action, Mapping):
            raise _error("validation_error", "action must be an object")
        kind = action.get("kind", action.get("type"))
        contract = self._contract(session)
        allowed = set(contract.permissions.allowed_tools) & self.policy.allowed_tools
        if kind == "browser":
            plan = self._normalize_browser_action(action)
            if plan.tool not in allowed:
                raise _error(
                    "action_blocked",
                    f"tool is outside effective policy: {plan.tool}",
                )
            return plan
        if kind == "http":
            if "http" not in allowed:
                raise _error(
                    "action_blocked",
                    "tool is outside effective policy: http",
                )
            return self._normalize_http_action(contract, action)
        if kind == "file_read":
            path = self._resolve_project_path(action.get("path", ""), field_name="path")
            tool, action_kind = "file.read", "tool_call"
            descriptor = {"kind": kind, "path": str(path)}
            description = f"read file {path}"
        elif kind == "file_write":
            path = self._resolve_project_path(action.get("path", ""), field_name="path")
            content = action.get("content")
            if not isinstance(content, str):
                raise _error("validation_error", "file_write.content must be a string")
            scopes = tuple(contract.permissions.write_scope)
            if not scopes:
                raise _error("action_blocked", "empty write_scope grants no MCP write")
            resolved_scopes = tuple(
                self._resolve_project_path(entry, field_name="write_scope")
                for entry in scopes
            )
            if not any(_within(path, scope) for scope in resolved_scopes):
                raise _error("action_blocked", "file path is outside write_scope")
            tool, action_kind = "file.write", "write"
            descriptor = {
                "kind": kind,
                "path": str(path),
                "content": content,
            }
            description = f"write file {path}"
        elif kind == "subprocess":
            raw_argv = action.get("argv")
            if isinstance(raw_argv, (str, bytes)) or not isinstance(raw_argv, (list, tuple)):
                raise _error("validation_error", "subprocess.argv must be a string array")
            argv = list(raw_argv)
            if not argv or any(not isinstance(item, str) or not item for item in argv):
                raise _error("validation_error", "subprocess.argv must contain non-blank strings")
            if not self.policy.allows_subprocess(tuple(argv)):
                raise _error(
                    "policy_denied",
                    "subprocess argv is outside the server-owned allowlist",
                )
            cwd = self._resolve_project_path(action.get("cwd", "."), field_name="cwd")
            timeout = action.get("timeout_seconds", action.get("timeout", 30.0))
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise _error("validation_error", "timeout must be numeric")
            timeout = float(timeout)
            if timeout <= 0 or timeout > self.policy.max_timeout_seconds:
                raise _error("validation_error", "timeout is outside server policy")
            tool, action_kind = "shell", self._subprocess_action_kind(argv)
            descriptor = {
                "kind": kind,
                "argv": argv,
                "cwd": str(cwd),
                "timeout_seconds": timeout,
            }
            description = "run argv: " + " ".join(argv)
        else:
            raise _error("validation_error", "unknown action kind")
        if tool not in allowed:
            raise _error("action_blocked", f"tool is outside effective policy: {tool}")
        return _ActionPlan(descriptor, tool, action_kind, description)

    def _enforce_current_http_policy(self, plan: _ActionPlan) -> None:
        if plan.http_request is None:
            return
        if plan.network_origin not in self.policy.allowed_network_origins:
            raise _error(
                "policy_denied",
                "HTTP origin is outside the current server policy",
            )
        if plan.auth_ref is not None and plan.auth_ref not in self.policy.allowed_auth_refs:
            raise _error(
                "policy_denied",
                "credential alias is outside the current server policy",
            )

    def _browser_context(
        self,
        task_id: str,
        contract: GoalContract,
    ) -> BrowserContext:
        session_id = hashlib.sha256(
            (f"{str(self.project_root).casefold()}\0{task_id}").encode("utf-8")
        ).hexdigest()
        profile = self._browser_runtime_directory("sessions", session_id)
        staging = self._browser_runtime_directory("staging", session_id)
        return BrowserContext(
            session_id,
            profile,
            tuple(contract.permissions.network_scope),
            staging,
        )

    def _browser_runtime_directory(self, *parts: str) -> Path:
        directory = self.project_root / ".causality"
        for part in (None, "browser", *parts):
            if part is not None:
                directory /= part
            try:
                directory.mkdir(parents=True, exist_ok=True)
                self._browser_directory_identity(directory)
            except OSError as exc:
                raise _error(
                    "browser_runtime_invalid",
                    "browser runtime directory is unavailable",
                ) from exc
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        return directory

    def _browser_directory_identity(self, directory: Path) -> tuple[int, int]:
        status = directory.lstat()
        if not stat.S_ISDIR(status.st_mode) or not _within(
            directory.resolve(), self.project_root
        ):
            raise _error(
                "browser_runtime_invalid",
                "browser runtime directory escapes the project",
            )
        return status.st_dev, status.st_ino

    def _browser_internal_path(
        self,
        category: str,
        context: BrowserContext,
        operation_id: str,
        suffix: str,
    ) -> Path:
        directory = self._browser_runtime_directory(category, context.session_id)
        return directory / f"{operation_id}{suffix}"

    def _write_browser_cache(
        self,
        context: BrowserContext,
        operation_id: str,
        payload: Mapping[str, str],
    ) -> dict[str, object]:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        size = len(encoded.encode("utf-8"))
        if size > _MAX_BROWSER_CACHE_BYTES:
            raise _error(
                "browser_output_too_large",
                "browser replay cache exceeds the server limit",
            )
        directory = self._browser_runtime_directory(
            "observations", context.session_id
        )
        parent_identity = self._browser_directory_identity(directory)
        descriptor = -1
        path: Path | None = None
        try:
            descriptor, raw_path = _browser_mkstemp(
                dir=directory,
                prefix=f"{operation_id}.",
                suffix=".json",
            )
            path = Path(raw_path)
            if self._browser_directory_identity(directory) != parent_identity:
                raise _error(
                    "browser_runtime_invalid",
                    "browser runtime directory changed during cache creation",
                )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                opened = os.fstat(handle.fileno())
            if self._browser_directory_identity(directory) != parent_identity:
                raise _error(
                    "browser_runtime_invalid",
                    "browser runtime directory changed during cache write",
                )
            written = path.lstat()
            if (
                not stat.S_ISREG(written.st_mode)
                or written.st_nlink != 1
                or (written.st_dev, written.st_ino) != (opened.st_dev, opened.st_ino)
                or not _within(path.resolve(), self.project_root)
            ):
                raise _error(
                    "browser_runtime_invalid",
                    "browser cache file changed during write",
                )
            return {
                "path": str(path),
                "bytes": size,
                "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            }
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            if path is not None:
                path.unlink(missing_ok=True)
            raise

    def _read_browser_cache(self, response: Mapping[str, Any]) -> dict[str, str]:
        cache = response.get("cache")
        if cache is None:
            return {}
        if not isinstance(cache, Mapping):
            raise _error("browser_cache_invalid", "browser replay cache metadata is invalid")
        raw_path = cache.get("path")
        expected_size = cache.get("bytes")
        expected_hash = cache.get("sha256")
        if (
            not isinstance(raw_path, str)
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or not isinstance(expected_hash, str)
            or not _SHA256.fullmatch(expected_hash)
        ):
            raise _error("browser_cache_invalid", "browser replay cache metadata is invalid")
        path = Path(raw_path)
        root = self.project_root / ".causality" / "browser" / "observations"
        try:
            if not _within(root.resolve(), self.project_root):
                raise _error(
                    "browser_cache_invalid", "browser replay root escapes the project"
                )
            if not _within(path.resolve(), root.resolve()):
                raise _error(
                    "browser_cache_invalid", "browser replay cache escapes runtime state"
                )
            status = path.lstat()
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise _error(
                    "browser_cache_invalid", "browser replay cache is not a private file"
                )
            if status.st_size != expected_size or status.st_size > _MAX_BROWSER_CACHE_BYTES:
                raise _error(
                    "browser_cache_invalid", "browser replay cache size changed"
                )
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    raise _error(
                        "browser_cache_invalid",
                        "browser replay cache changed while opening",
                    )
                raw = handle.read(_MAX_BROWSER_CACHE_BYTES + 1)
        except FileNotFoundError as exc:
            raise _error(
                "browser_cache_invalid",
                "browser replay cache is missing; use a new idempotency key",
            ) from exc
        except OSError as exc:
            raise _error(
                "browser_cache_invalid", "browser replay cache cannot be read"
            ) from exc
        if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_hash:
            raise _error("browser_cache_invalid", "browser replay cache hash changed")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _error("browser_cache_invalid", "browser replay cache is invalid JSON") from exc
        if not isinstance(value, dict) or any(
            not isinstance(name, str) or not isinstance(item, str)
            for name, item in value.items()
        ):
            raise _error("browser_cache_invalid", "browser replay cache payload is invalid")
        return value

    def _latest_browser_state_hash(self, task_id: str) -> str | None:
        for event in reversed(
            self.ledger.events_for_contract(task_id, all_segments=True)
        ):
            if event.event_type != TASK_ACTION_RESULT:
                continue
            descriptor = event.payload.get("descriptor")
            response = event.payload.get("response")
            if (
                not isinstance(descriptor, Mapping)
                or descriptor.get("kind") != "browser"
                or not isinstance(response, Mapping)
            ):
                continue
            for field_name in ("after_state_hash", "state_hash"):
                value = response.get(field_name)
                if isinstance(value, str) and _SHA256.fullmatch(value):
                    return value
        return None

    @staticmethod
    def _browser_text_diff(before: str, after: str) -> str:
        return "\n".join(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile="before",
                tofile="after",
                lineterm="",
            )
        )

    def _current_browser_observation(
        self,
        task_id: str,
        plan: _BrowserPlan,
        context: BrowserContext,
    ):
        assert self.browser_adapter is not None
        latest = self._latest_browser_state_hash(task_id)
        if latest is None or latest != plan.expected_state_hash:
            raise _error(
                "browser_state_mismatch",
                "expected_state_hash is not the latest task browser observation",
            )
        current = self.browser_adapter.observe("interactive", context=context)
        if current.state_hash != plan.expected_state_hash:
            raise _error(
                "browser_state_mismatch",
                "browser state changed; observe again with a new idempotency key",
            )
        if plan.ref is not None and plan.ref not in set(REF_RE.findall(current.snapshot)):
            raise _error(
                "browser_state_mismatch",
                "stable ref is absent from the current browser observation",
            )
        return current

    def _execute_browser_action(
        self,
        task_id: str,
        operation_id: str,
        plan: _BrowserPlan,
        contract: GoalContract,
        before_effect: Callable[[], None],
    ) -> tuple[dict[str, Any], dict[str, str], tuple[Path, ...]]:
        adapter = self.browser_adapter
        if adapter is None:
            raise _error("policy_denied", "browser driver is not configured")
        context = self._browser_context(task_id, contract)
        operation = plan.operation
        artifact_paths: list[Path] = []
        ephemeral: dict[str, str] = {}

        if operation == "observe":
            annotated = (
                self._browser_internal_path(
                    "artifacts", context, operation_id, ".annotated.png"
                )
                if plan.annotate
                else None
            )
            before_effect()
            observation = adapter.observe(
                plan.mode,
                scope=plan.scope,
                annotate_path=annotated,
                context=context,
            )
            canonical = (
                observation
                if plan.mode == "interactive" and plan.scope is None
                else adapter.observe("interactive", context=context)
            )
            ephemeral = {"snapshot": observation.snapshot}
            cache = self._write_browser_cache(context, operation_id, ephemeral)
            artifact_paths.append(Path(str(cache["path"])))
            artifact_paths.extend(Path(item.path) for item in observation.artifacts)
            metadata = observation.to_metadata()
            metadata.update(
                {
                    "snapshot_hash": observation.state_hash,
                    "state_hash": canonical.state_hash,
                    "state_line_count": canonical.line_count,
                    "state_ref_count": canonical.ref_count,
                }
            )
            result = {
                "kind": "browser",
                "operation": operation,
                **metadata,
                "cache": cache,
            }
            return result, ephemeral, tuple(artifact_paths)

        current = self._current_browser_observation(task_id, plan, context)
        if operation == "act":
            before_diagnostics = adapter.diagnostics(context=context)
            before_effect()
            adapter.act(
                BrowserAction(plan.ref or "", plan.action or "click", plan.value),
                context=context,
            )
            after = adapter.observe("interactive", context=context)
            after_diagnostics = adapter.diagnostics(context=context)
            ephemeral = {
                "after_snapshot": after.snapshot,
                "diff": self._browser_text_diff(current.snapshot, after.snapshot),
                "console_delta": self._browser_text_diff(
                    before_diagnostics.console, after_diagnostics.console
                ),
                "network_delta": self._browser_text_diff(
                    before_diagnostics.network, after_diagnostics.network
                ),
            }
            cache = self._write_browser_cache(context, operation_id, ephemeral)
            artifact_paths.append(Path(str(cache["path"])))
            result = {
                "kind": "browser",
                "operation": operation,
                "action": plan.action,
                "ref": plan.ref,
                "before_state_hash": current.state_hash,
                "after_state_hash": after.state_hash,
                "changed": current.state_hash != after.state_hash,
                "after_line_count": after.line_count,
                "after_ref_count": after.ref_count,
                "diff_bytes": utf8_size(ephemeral["diff"]),
                "diff_sha256": sha256_text(ephemeral["diff"]),
                "console_delta_bytes": utf8_size(ephemeral["console_delta"]),
                "console_delta_sha256": sha256_text(ephemeral["console_delta"]),
                "network_delta_bytes": utf8_size(ephemeral["network_delta"]),
                "network_delta_sha256": sha256_text(ephemeral["network_delta"]),
                "cache": cache,
            }
            return result, ephemeral, tuple(artifact_paths)

        before_effect()
        if operation == "assert":
            command = adapter.assert_state(
                plan.assertion or "visible", plan.ref or "", context=context
            )
            ephemeral = {"assertion_output": command.stdout.strip()}
            cache = self._write_browser_cache(context, operation_id, ephemeral)
            artifact_paths.append(Path(str(cache["path"])))
            result = {
                "kind": "browser",
                "operation": operation,
                "property": plan.assertion,
                "ref": plan.ref,
                "state_hash": current.state_hash,
                "passed": command.stdout.strip().casefold() in {"true", "1", "yes", "ok"},
                "output_bytes": utf8_size(command.stdout),
                "output_sha256": sha256_text(command.stdout),
                "cache": cache,
            }
        elif operation == "inspect":
            command = adapter.inspect(
                plan.ref or "", plan.inspection or "attrs", context=context
            )
            ephemeral = {"inspection": command.stdout}
            cache = self._write_browser_cache(context, operation_id, ephemeral)
            artifact_paths.append(Path(str(cache["path"])))
            result = {
                "kind": "browser",
                "operation": operation,
                "inspection": plan.inspection,
                "ref": plan.ref,
                "state_hash": current.state_hash,
                "output_bytes": utf8_size(command.stdout),
                "output_sha256": sha256_text(command.stdout),
                "cache": cache,
            }
        else:
            artifact_path = self._browser_internal_path(
                "artifacts", context, operation_id, ".png"
            )
            artifact = adapter.visual(
                artifact_path,
                target_ref=plan.ref,
                context=context,
            )
            artifact_paths.append(Path(artifact.path))
            result = {
                "kind": "browser",
                "operation": operation,
                "ref": plan.ref,
                "state_hash": current.state_hash,
                "artifact": artifact.to_metadata(),
            }
        return result, ephemeral, tuple(artifact_paths)

    def _record_browser_provenance(
        self,
        task_id: str,
        operation_id: str,
        plan: _ActionPlan,
        contract: GoalContract,
        result: Mapping[str, Any],
        artifact_paths: tuple[Path, ...],
    ) -> tuple[str, ...]:
        assert plan.browser is not None
        operation = plan.browser.operation
        event = self.ledger.append(
            (
                AuditEventType.BROWSER_ACTION
                if operation == "act"
                else AuditEventType.BROWSER_OBSERVATION
            ),
            {
                "schema_version": SCHEMA_VERSION,
                "task_id": task_id,
                "operation_id": operation_id,
                "tool": plan.tool,
                "action_kind": plan.action_kind,
                "operation": operation,
                "result_sha256": canonical_sha256(result),
                "result": dict(result),
                "mutates_task": operation == "act",
            },
            contract_id=task_id,
            artifact_paths=artifact_paths,
        )
        kinds = {
            "observe": (EvidenceKind.A11Y_REPORT,),
            "act": (EvidenceKind.BROWSER_DIFF, EvidenceKind.A11Y_REPORT),
            "assert": (EvidenceKind.A11Y_REPORT,),
            "inspect": (EvidenceKind.A11Y_REPORT,),
            "visual": (EvidenceKind.ARTIFACT_HASH,),
        }[operation]
        hashes = [event.entry_hash]
        for kind in kinds:
            evidence = self.runtime.record_evidence(
                contract,
                kind,
                {
                    "task_id": task_id,
                    "operation_id": operation_id,
                    "operation": operation,
                    "result_sha256": canonical_sha256(result),
                    "browser_event_hash": event.entry_hash,
                },
                artifact_paths,
            )
            hashes.append(evidence.entry_hash)
        return tuple(hashes)

    def action(
        self,
        task_id: str,
        action: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> TaskSession:
        return self.perform_action(
            task_id,
            action,
            idempotency_key=idempotency_key,
        ).session

    def perform_action(
        self,
        task_id: str,
        action: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> TaskActionReceipt:
        key = _idempotency_key(idempotency_key)
        with self.runtime.execution_lock():
            session = self.get(task_id)
            http_digest: str | None = None
            if isinstance(action, Mapping) and action.get("kind") == "http":
                try:
                    http_digest = canonical_sha256(action)
                except (TypeError, ValueError) as exc:
                    raise _error(
                        "validation_error",
                        "HTTP action is not canonically serializable",
                    ) from exc
                prior = self._existing(session, "action", key, http_digest)
                if prior is not None:
                    if prior.outcome == "error" and session.state is TaskState.EXECUTING:
                        self._append_transition(
                            session,
                            TaskState.BLOCKED,
                            reason="action outcome is uncertain",
                            cause_event_hash=prior.event_hashes[-1],
                        )
                        session = self.get(task_id)
                    response = (
                        _thaw(prior.response)
                        if isinstance(prior.response, Mapping)
                        else {}
                    )
                    return TaskActionReceipt(session, response, replayed=True)
            plan = self._normalize_action(session, action)
            descriptor = plan.descriptor
            tool = plan.tool
            action_kind = plan.action_kind
            description = plan.description
            digest = http_digest or canonical_sha256(descriptor)
            if http_digest is None:
                prior = self._existing(session, "action", key, digest)
                if prior is not None:
                    if prior.outcome == "error" and session.state is TaskState.EXECUTING:
                        self._append_transition(
                            session,
                            TaskState.BLOCKED,
                            reason="action outcome is uncertain",
                            cause_event_hash=prior.event_hashes[-1],
                        )
                        session = self.get(task_id)
                    response = (
                        _thaw(prior.response)
                        if isinstance(prior.response, Mapping)
                        else {}
                    )
                    ephemeral = (
                        self._read_browser_cache(response)
                        if plan.browser is not None
                        else {}
                    )
                    return TaskActionReceipt(session, response, ephemeral, replayed=True)
            self._require_running_workflow_phase(session, "action")
            self._enforce_current_http_policy(plan)
            if plan.browser is not None:
                contract = self._contract(session)
                denied_origins = (
                    set(contract.permissions.network_scope)
                    - self.policy.allowed_network_origins
                )
                if (
                    self.browser_adapter is None
                    or plan.tool not in self.policy.allowed_tools
                    or denied_origins
                ):
                    raise _error(
                        "policy_denied",
                        "browser capability or network scope is no longer available",
                    )
            session = self._ensure_executing(session)
            operation_id = self._operation_id(task_id, "action", key, digest)
            intent: LedgerEvent | None = None

            def before_effect() -> None:
                nonlocal intent
                if intent is not None:
                    raise RuntimeError("effect hook executed more than once")
                intent = self.ledger.append(
                    AuditEventType.TASK_ACTION_INTENT,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "task_id": task_id,
                        "operation": "action",
                        "operation_id": operation_id,
                        "idempotency_key": key,
                        "request_sha256": digest,
                        "descriptor": descriptor,
                    },
                    contract_id=task_id,
                )

            execution_contract = self._contract(session)
            execution = ExecutionAdapter(self.runtime, execution_contract)
            provenance: tuple[str, ...] = ()
            ephemeral: dict[str, str] = {}
            browser_artifact_paths: tuple[Path, ...] = ()
            try:
                if plan.http_request is not None:
                    def run_http() -> Mapping[str, Any]:
                        credential_headers = None
                        if plan.auth_ref is not None:
                            credential_headers = self.http_credentials.get(plan.auth_ref)
                            if credential_headers is None:
                                raise _error(
                                    "policy_denied",
                                    "credential alias is not configured",
                                )
                            collisions = {
                                name.casefold() for name in plan.http_request.headers
                            } & {name.casefold() for name in credential_headers}
                            if collisions:
                                raise _error(
                                    "validation_error",
                                    "caller headers conflict with server credentials",
                                )
                        response = self.http_adapter.send(
                            plan.http_request,
                            credential_headers=credential_headers,
                            before_effect=before_effect,
                        )
                        return {
                            **response.to_metadata(),
                            "expected": response.status in plan.expected_statuses,
                            "expected_statuses": list(plan.expected_statuses),
                        }

                    raw = execution.execute(
                        tool=tool,
                        action_kind=action_kind,
                        description=description,
                        network_origin=plan.network_origin,
                        auth_ref=plan.auth_ref,
                        run=run_http,
                    )
                    result = _thaw(_freeze(dict(raw)))
                    artifact_paths = (
                        (plan.http_request.artifact_path,)
                        if plan.http_request.artifact_path is not None
                        else ()
                    )
                    event = self.ledger.append(
                        AuditEventType.TOOL_CALL,
                        {
                            "tool": tool,
                            "action_kind": action_kind,
                            "result_sha256": canonical_sha256(result),
                            "status": result["status"],
                            "expected": result["expected"],
                            "request_bytes": result["request_bytes"],
                            "response_bytes": result["response_bytes"],
                            "response_sha256": result["response_sha256"],
                            "mutates_task": True,
                        },
                        contract_id=task_id,
                        artifact_paths=artifact_paths,
                    )
                    provenance = (event.entry_hash,)
                elif plan.browser is not None:
                    browser_contract = execution_contract

                    def run_browser() -> Mapping[str, Any]:
                        nonlocal ephemeral, browser_artifact_paths
                        result, ephemeral, browser_artifact_paths = self._execute_browser_action(
                            task_id,
                            operation_id,
                            plan.browser,
                            browser_contract,
                            before_effect,
                        )
                        return result

                    raw = execution.execute(
                        tool=tool,
                        action_kind=action_kind,
                        description=description,
                        network_origins=browser_contract.permissions.network_scope,
                        run=run_browser,
                    )
                    if not isinstance(raw, Mapping):
                        raise ValueError("browser adapter must return a mapping")
                    result = _thaw(_freeze(dict(raw)))
                    provenance = self._record_browser_provenance(
                        task_id,
                        operation_id,
                        plan,
                        browser_contract,
                        result,
                        browser_artifact_paths,
                    )
                elif self.effect_runner is not None:
                    def run_injected() -> Mapping[str, Any]:
                        before_effect()
                        return self.effect_runner(_thaw(_freeze(descriptor)))

                    raw = execution.execute(
                        tool=tool,
                        action_kind=action_kind,
                        description=description,
                        run=run_injected,
                        network_origin=plan.network_origin,
                        auth_ref=plan.auth_ref,
                    )
                    if not isinstance(raw, Mapping):
                        raise ValueError("effect_runner must return a mapping")
                    result = _thaw(_freeze(dict(raw)))
                    canonical_sha256(result)
                    event = self.ledger.append(
                        AuditEventType.TOOL_CALL,
                        {
                            "tool": tool,
                            "action_kind": action_kind,
                            "result_sha256": canonical_sha256(result),
                            "mutates_task": descriptor["kind"] != "file_read",
                        },
                        contract_id=task_id,
                    )
                    provenance = (event.entry_hash,)
                else:
                    tools = ToolAdapter(self.ledger, execution, root=self.project_root)
                    if descriptor["kind"] == "file_read":
                        content = tools.read_text(descriptor["path"], before_effect=before_effect)
                        result = {
                            "kind": "file_read",
                            "path": descriptor["path"],
                            "bytes": utf8_size(content),
                            "sha256": sha256_text(content),
                        }
                    elif descriptor["kind"] == "file_write":
                        target = tools.write_text(
                            descriptor["path"],
                            descriptor["content"],
                            before_effect=before_effect,
                        )
                        result = {
                            "kind": "file_write",
                            "path": str(target),
                            "bytes": utf8_size(descriptor["content"]),
                            "sha256": sha256_file(target),
                        }
                    else:
                        completed = tools.run(
                            descriptor["argv"],
                            timeout=descriptor["timeout_seconds"],
                            cwd=descriptor["cwd"],
                            action_kind=action_kind,
                            before_effect=before_effect,
                        )
                        result = {
                            "kind": "subprocess",
                            "exit_code": completed.exit_code,
                            "stdout_bytes": utf8_size(completed.stdout),
                            "stderr_bytes": utf8_size(completed.stderr),
                            "stdout_sha256": sha256_text(completed.stdout),
                            "stderr_sha256": sha256_text(completed.stderr),
                        }
                    provenance = (
                        (tools.last_event_hash,) if tools.last_event_hash is not None else ()
                    )
            except ActionBlocked as exc:
                raise _error(
                    "approval_required"
                    if exc.result.decision is GateDecision.ESCALATE
                    else "action_blocked",
                    str(exc),
                ) from exc
            except TaskLifecycleError:
                raise
            except Exception as exc:
                if intent is not None:
                    self.ledger.append(
                        AuditEventType.TOOL_CALL,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "task_id": task_id,
                            "tool": tool,
                            "action_kind": action_kind,
                            "operation_id": operation_id,
                            "idempotency_key": key,
                            "request_sha256": digest,
                            "outcome": "uncertain",
                            "error_type": type(exc).__name__,
                            "mutates_task": True,
                        },
                        contract_id=task_id,
                    )
                if plan.http_request is not None:
                    phase = "after intent" if intent is not None else "before effect"
                    message = f"HTTP action failed {phase}: {type(exc).__name__}"
                elif plan.browser is not None:
                    phase = "after intent" if intent is not None else "before effect"
                    message = f"browser action failed {phase}: {type(exc).__name__}"
                else:
                    message = f"action failed: {type(exc).__name__}: {exc}"
                raise _error("action_failed", message) from exc

            if intent is None or not provenance:
                raise RuntimeError("action completed without durable intent/provenance")
            self.ledger.append(
                AuditEventType.TASK_ACTION_RESULT,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "operation": "action",
                    "operation_id": operation_id,
                    "idempotency_key": key,
                    "request_sha256": digest,
                    "descriptor": descriptor,
                    "outcome": "completed",
                    "result": result,
                    "response": result,
                    "provenance_event_hashes": list(provenance),
                },
                contract_id=task_id,
            )
            return TaskActionReceipt(
                self.get(task_id),
                result,
                ephemeral,
                replayed=False,
            )

    def _durable_state(self, task_id: str) -> TaskState:
        snapshot = self.ledger.contract_snapshot(task_id)
        if snapshot is None:
            raise _error("task_not_found", f"task not found: {task_id}")
        state = TaskState(snapshot.get("state", TaskState.PLANNED.value))
        for event in self.ledger.events_for_contract(task_id, all_segments=True):
            if event.event_type == STATE_TRANSITION:
                state = TaskState(event.payload["state"])
        return state

    def resolve(
        self,
        task_id: str,
        *,
        operation_id: str,
        resolution: str,
        approver: str,
        rationale: str,
        idempotency_key: str,
        proof: str | None = None,
    ) -> TaskSession:
        key = _idempotency_key(idempotency_key)
        if resolution not in {"applied", "not_applied", "reject"}:
            raise _error("validation_error", "invalid recovery resolution")
        if not isinstance(approver, str) or not approver.strip():
            raise _error("validation_error", "approver must be non-blank")
        if not isinstance(rationale, str) or not rationale.strip():
            raise _error("validation_error", "rationale must be non-blank")
        request = {
            "operation_id": operation_id,
            "resolution": resolution,
            "approver": approver.strip(),
            "rationale": rationale.strip(),
        }
        digest = canonical_sha256(request)
        with self.runtime.execution_lock():
            session = self.get(task_id)
            events = self.ledger.events_for_contract(task_id, all_segments=True)
            completed_recoveries = {
                event.payload.get("operation_id")
                for event in events
                if event.event_type == TASK_OPERATION
                and event.payload.get("operation") == "resolve"
            }
            pending_recoveries = [
                event
                for event in events
                if event.event_type == AuditEventType.HUMAN_DECISION.value
                and event.payload.get("task_id") == task_id
                and event.payload.get("stage") == "recovery"
                and event.payload.get("operation") in {None, "resolve"}
                and event.payload.get("operation_id") not in completed_recoveries
            ]
            if len(pending_recoveries) > 1:
                raise _error(
                    "invalid_recovery",
                    "task contains multiple unfinished recovery decisions",
                )
            if pending_recoveries:
                reserved = pending_recoveries[0].payload
                if (
                    reserved.get("idempotency_key") != key
                    or reserved.get("request_sha256") != digest
                ):
                    raise _error(
                        "recovery_in_progress",
                        "another trusted recovery decision already owns this effect",
                        operation_id=reserved.get("target_operation_id"),
                        idempotency_key=reserved.get("idempotency_key"),
                    )
            prior = self._existing(session, "resolve", key, digest)
            if prior is not None:
                if resolution == "not_applied" and session.state is TaskState.BLOCKED:
                    self._append_transition(
                        session,
                        TaskState.EXECUTING,
                        reason="operator confirmed effect was not applied",
                        cause_event_hash=prior.event_hashes[-1],
                    )
                    return self.get(task_id)
                if resolution == "reject" and session.state is TaskState.BLOCKED:
                    self._append_transition(
                        session,
                        TaskState.REJECTED,
                        reason="operator rejected task during recovery",
                        cause_event_hash=prior.event_hashes[-1],
                    )
                    return self.get(task_id)
                return session
            pending = next(
                (item for item in session.unresolved_intents if item.operation_id == operation_id),
                None,
            )
            if pending is None or pending.kind != "action":
                raise _error("unresolved_action_intent", "recovery target is not pending")
            stored = self._durable_state(task_id)
            if stored is not TaskState.BLOCKED:
                base = replace(session, state=stored)
                self._append_transition(
                    base,
                    TaskState.BLOCKED,
                    reason="unresolved action intent requires recovery",
                    cause_event_hash=pending.event_hash,
                )
                session = self.get(task_id)
            decision = (
                pending_recoveries[0]
                if pending_recoveries
                else self._authoritative_event(
                    task_id,
                    "resolve",
                    key,
                    digest,
                    AuditEventType.HUMAN_DECISION,
                )
            )
            if decision is None:
                self._authorize(approver.strip(), "recovery", proof)
                decision = self.ledger.append(
                    AuditEventType.HUMAN_DECISION,
                    {
                        **self._operation_metadata(
                            task_id,
                            "resolve",
                            key,
                            digest,
                        ),
                        "stage": "recovery",
                        "approved": resolution != "reject",
                        "approver": approver.strip(),
                        "rationale": rationale.strip(),
                        "resolution": resolution,
                        "target_operation_id": operation_id,
                    },
                    contract_id=task_id,
                )
            operation = self._append_operation(
                session,
                "resolve",
                key,
                digest,
                request,
                {
                    "operation_id": operation_id,
                    "resolution": resolution,
                    "decision_event_hash": decision.entry_hash,
                },
            )
            session = self.get(task_id)
            if resolution == "not_applied":
                self._append_transition(
                    session,
                    TaskState.EXECUTING,
                    reason="operator confirmed effect was not applied",
                    cause_event_hash=operation.entry_hash,
                )
            elif resolution == "reject":
                self._append_transition(
                    session,
                    TaskState.REJECTED,
                    reason="operator rejected task during recovery",
                    cause_event_hash=operation.entry_hash,
                )
            return self.get(task_id)

    def _authorize(
        self,
        approver: str,
        stage: str,
        proof: str | None,
    ) -> None:
        if not self.approval_authorizer(approver, stage, proof):
            raise _error("approval_required", "approval proof was not trusted")

    def _scoped_hashes(self, task_id: str) -> set[str]:
        return {
            event.entry_hash
            for event in self.ledger.events_for_contract(task_id, all_segments=True)
        }

    def _validate_evidence_refs(
        self,
        task_id: str,
        evidence_refs: tuple[str, ...],
    ) -> None:
        if len(evidence_refs) != len(set(evidence_refs)):
            raise _error("evidence_scope_mismatch", "evidence_refs must be unique")
        scoped = self._scoped_hashes(task_id)
        if any(not _SHA256.fullmatch(ref) or ref not in scoped for ref in evidence_refs):
            raise _error(
                "evidence_scope_mismatch",
                "evidence reference is not in this task",
            )

    def _current_evidence(self, session: TaskSession) -> tuple[str, ...]:
        contract = self._contract(session)
        events = self.ledger.events_for_contract(session.task_id, all_segments=True)
        last_mutation = max(
            (
                index
                for index, event in enumerate(events)
                if event.payload.get("mutates_task") is True
            ),
            default=-1,
        )
        issues, _, hashes = self.runtime.gate._structured_requirement_issues(
            contract.verification_requirements,
            events,
            workspace_root=contract.workspace_root,
            last_mutation=last_mutation,
        )
        generic_issues, _, generic_hashes = (
            self.runtime.gate._structured_generic_evidence_issues(
                contract,
                events,
                workspace_root=contract.workspace_root,
                last_mutation=last_mutation,
            )
        )
        issues.extend(generic_issues)
        hashes.update(generic_hashes)
        if issues or not hashes:
            raise _error(
                "evidence_scope_mismatch",
                "current required evidence is incomplete",
                issues=issues,
            )
        return tuple(sorted(hashes))

    @staticmethod
    def _running_workflow_phase(session: TaskSession) -> WorkflowPhase | None:
        return next(
            (phase for phase in session.workflow_phases if phase.status == "running"),
            None,
        )

    def _require_running_workflow_phase(
        self,
        session: TaskSession,
        operation: str,
    ) -> WorkflowPhase | None:
        if not session.workflow_phases:
            return None
        phase = self._running_workflow_phase(session)
        if phase is None and operation in {"verification", "verdict", "evidence"}:
            if all(item.status == "passed" for item in session.workflow_phases):
                return None
        if phase is None:
            raise _error(
                "phase_not_running",
                f"workflow phase must be running before {operation}",
            )
        return phase

    def _phase_evidence(
        self,
        session: TaskSession,
        phase: WorkflowPhase,
        refs: tuple[str, ...],
        status: str,
    ) -> tuple[str, ...]:
        events = self.ledger.events_for_contract(
            session.task_id,
            all_segments=True,
        )
        start_index = -1
        for index, event in enumerate(events):
            if event.event_type != TASK_OPERATION or event.payload.get("operation") != "phase":
                continue
            response = event.payload.get("response")
            phase_value = response.get("phase") if isinstance(response, Mapping) else None
            if (
                isinstance(phase_value, Mapping)
                and phase_value.get("phase_id") == phase.phase_id
                and phase_value.get("status") == "running"
                and phase_value.get("attempt") == phase.attempt
            ):
                start_index = index
        if start_index < 0:
            raise _error("invalid_task_event", "running phase has no start event")
        return _validate_phase_evidence_events(
            events,
            phase,
            refs,
            start_index=start_index,
            status=status,
        )

    @staticmethod
    def _current_hypothesis_records(
        session: TaskSession,
    ) -> tuple[IdempotencyRecord, ...]:
        positions = {
            event_hash: index
            for index, event_hash in enumerate(session.event_hashes)
        }
        last_approval = max(
            (
                positions.get(record.event_hashes[-1], -1)
                for record in session.idempotency.values()
                if record.operation == "approve"
                and isinstance(record.response, Mapping)
                and record.response.get("stage") == "phase"
                and record.response.get("approved") is True
            ),
            default=-1,
        )
        return tuple(
            sorted(
                (
                    record
                    for record in session.idempotency.values()
                    if record.operation == "hypothesis"
                    and positions.get(record.event_hashes[-1], -1) > last_approval
                ),
                key=lambda record: positions.get(record.event_hashes[-1], -1),
            )
        )

    def _hypothesis_block_record(
        self,
        session: TaskSession,
    ) -> IdempotencyRecord | None:
        maximum = int(
            self._contract(session).stopping_policy.get(
                "max_failed_hypotheses",
                0,
            )
            or 0
        )
        if maximum < 1:
            return None
        return next(
            (
                record
                for record in reversed(self._current_hypothesis_records(session))
                if isinstance(record.response, Mapping)
                and record.response.get("failed_hypotheses", 0) >= maximum
            ),
            None,
        )

    @classmethod
    def _failed_hypotheses(cls, session: TaskSession) -> int:
        return sum(
            1
            for record in cls._current_hypothesis_records(session)
            if isinstance(record.response, Mapping)
            and record.response.get("status") == "rejected"
        )

    def _reconcile_hypothesis_block(self, session: TaskSession) -> TaskSession:
        record = self._hypothesis_block_record(session)
        if record is None:
            return session
        failed = int(record.response["failed_hypotheses"])
        gate_event = self._authoritative_event(
            session.task_id,
            "hypothesis",
            record.idempotency_key,
            record.request_sha256,
            AuditEventType.GATE_DECISION,
        )
        if gate_event is None:
            gate = self.runtime.should_stop(
                self._contract(session),
                {"failed_hypotheses": failed},
                event_metadata=self._operation_metadata(
                    session.task_id,
                    "hypothesis",
                    record.idempotency_key,
                    record.request_sha256,
                ),
            )
            if gate.decision is not GateDecision.ESCALATE:
                raise _error(
                    "invalid_task_event",
                    "hypothesis threshold did not produce escalation",
                )
            gate_event = self._authoritative_event(
                session.task_id,
                "hypothesis",
                record.idempotency_key,
                record.request_sha256,
                AuditEventType.GATE_DECISION,
            )
        if gate_event is None:
            raise _error("invalid_task_event", "hypothesis escalation is missing")
        durable_state = self._durable_state(session.task_id)
        if durable_state is TaskState.BLOCKED:
            return self.get(session.task_id)
        if durable_state is not TaskState.EXECUTING:
            raise _error(
                "invalid_transition",
                "hypothesis escalation requires executing state",
            )
        self._append_transition(
            replace(session, state=durable_state),
            TaskState.BLOCKED,
            reason="failed hypothesis limit requires human review",
            cause_event_hash=gate_event.entry_hash,
        )
        return self.get(session.task_id)

    def _phase_block_event(self, session: TaskSession) -> LedgerEvent | None:
        current = session.current_phase_id
        events = self.ledger.events_for_contract(
            session.task_id,
            all_segments=True,
        )
        events_by_hash = {event.entry_hash: event for event in events}
        for event in reversed(events):
            response = event.payload.get("response")
            phase = response.get("phase") if isinstance(response, Mapping) else None
            if (
                event.event_type == TASK_OPERATION
                and event.payload.get("operation") == "phase"
                and isinstance(phase, Mapping)
                and phase.get("phase_id") == current
            ):
                if phase.get("status") == "blocked":
                    return event
                if phase.get("status") == "running":
                    return None
            if _verification_result_requires_phase_block(event, events_by_hash):
                return event
        return None

    def _reconcile_phase_block(
        self,
        session: TaskSession,
        *,
        expected_event_hash: str,
    ) -> TaskSession:
        if session.terminal:
            return session
        current = next(
            (
                phase
                for phase in session.workflow_phases
                if phase.phase_id == session.current_phase_id
            ),
            None,
        )
        if current is None or current.status != "blocked":
            return session
        hypothesis = self._hypothesis_block_record(session)
        if hypothesis is not None:
            if hypothesis.event_hashes[-1] != expected_event_hash:
                return session
            return self._reconcile_hypothesis_block(session)
        durable_state = self._durable_state(session.task_id)
        if durable_state is TaskState.BLOCKED:
            return session
        cause = self._phase_block_event(session)
        if cause is None or durable_state is not TaskState.EXECUTING:
            raise _error("invalid_task_event", "blocked phase has no durable cause")
        if cause.entry_hash != expected_event_hash:
            return session
        self._append_transition(
            replace(session, state=durable_state),
            TaskState.BLOCKED,
            reason="workflow phase requires human review",
            cause_event_hash=cause.entry_hash,
        )
        return self.get(session.task_id)

    def _phase_approval_evidence(
        self,
        session: TaskSession,
        *,
        allow_pending: bool = False,
    ) -> tuple[str, ...]:
        hypothesis = self._hypothesis_block_record(session)
        if hypothesis is not None:
            gate = self._authoritative_event(
                session.task_id,
                "hypothesis",
                hypothesis.idempotency_key,
                hypothesis.request_sha256,
                AuditEventType.GATE_DECISION,
            )
            if gate is None:
                if allow_pending:
                    return ()
                raise _error(
                    "invalid_task_event",
                    "blocked hypothesis phase is missing its escalation gate",
                )
            rejected = tuple(
                record.event_hashes[-1]
                for record in self._current_hypothesis_records(session)
                if isinstance(record.response, Mapping)
                and record.response.get("status") == "rejected"
            )
            return (*rejected, gate.entry_hash)
        cause_event = self._phase_block_event(session)
        cause = cause_event.entry_hash if cause_event is not None else None
        if cause is None:
            if allow_pending:
                return ()
            raise _error("invalid_task_event", "blocked phase has no approval evidence")
        return (cause,)

    def phase(
        self,
        task_id: str,
        *,
        phase_id: str,
        action: str,
        idempotency_key: str,
        status: str | None = None,
        evidence_refs: tuple[str, ...] = (),
    ) -> TaskSession:
        key = _idempotency_key(idempotency_key)
        if not isinstance(phase_id, str) or not phase_id.strip():
            raise _error("validation_error", "phase_id must be non-blank")
        phase_id = phase_id.strip()
        if action not in {"start", "finish"}:
            raise _error("validation_error", "phase action must be start or finish")
        refs = tuple(evidence_refs)
        request: dict[str, Any] = {"action": action, "phase_id": phase_id}
        if action == "start":
            if status is not None or refs:
                raise _error(
                    "validation_error",
                    "phase start does not accept status or evidence_refs",
                )
        else:
            if status not in {"passed", "failed", "blocked"}:
                raise _error(
                    "validation_error",
                    "phase finish status must be passed, failed, or blocked",
                )
            request.update({"status": status, "evidence_refs": list(refs)})
        digest = canonical_sha256(request)
        with self.runtime.execution_lock():
            session = self.get(task_id)
            prior = self._existing(session, "phase", key, digest)
            if prior is not None:
                return self._reconcile_phase_block(
                    session,
                    expected_event_hash=prior.event_hashes[-1],
                )
            if not session.workflow_phases:
                raise _error("phase_not_configured", "task has no workflow phase plan")
            current = next(
                (
                    phase
                    for phase in session.workflow_phases
                    if phase.phase_id == session.current_phase_id
                ),
                None,
            )
            if current is None:
                raise _error("workflow_complete", "all workflow phases already passed")
            if current.phase_id != phase_id:
                raise _error(
                    "phase_mismatch",
                    "phase_id does not match the current workflow phase",
                    expected=current.phase_id,
                    actual=phase_id,
                )
            if action == "start":
                if self._running_workflow_phase(session) is not None:
                    raise _error("phase_in_progress", "a workflow phase is already running")
                if current.status not in {"pending", "failed"}:
                    raise _error("phase_blocked", "current workflow phase cannot start")
                session = self._ensure_executing(session)
                phase_value = {
                    "phase_id": current.phase_id,
                    "from_status": current.status,
                    "status": "running",
                    "attempt": current.attempt + 1,
                    "evidence_hashes": [],
                }
            else:
                if current.status != "running":
                    raise _error("phase_not_running", "current workflow phase is not running")
                session = self._ensure_executing(session)
                evidence = self._phase_evidence(session, current, refs, status or "")
                phase_value = {
                    "phase_id": current.phase_id,
                    "from_status": current.status,
                    "status": status,
                    "attempt": current.attempt,
                    "evidence_hashes": list(evidence),
                }
            operation = self._append_operation(
                session,
                "phase",
                key,
                digest,
                request,
                {"phase": phase_value},
            )
            if status == "blocked":
                session = self.get(task_id)
                return self._reconcile_phase_block(
                    session,
                    expected_event_hash=operation.entry_hash,
                )
            return self.get(task_id)

    def hypothesis(
        self,
        task_id: str,
        *,
        phase_id: str,
        hypothesis: str,
        verifier: str,
        status: str,
        rationale: str,
        evidence_refs: tuple[str, ...],
        idempotency_key: str,
    ) -> TaskSession:
        key = _idempotency_key(idempotency_key)
        values = {
            "phase_id": phase_id,
            "hypothesis": hypothesis,
            "verifier": verifier,
            "rationale": rationale,
        }
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise _error("validation_error", "hypothesis text fields must be non-blank")
        if status not in {"supported", "rejected", "inconclusive"}:
            raise _error(
                "validation_error",
                "hypothesis status must be supported, rejected, or inconclusive",
            )
        refs = tuple(evidence_refs)
        request = {
            **{name: value.strip() for name, value in values.items()},
            "status": status,
            "evidence_refs": list(refs),
        }
        digest = canonical_sha256(request)
        with self.runtime.execution_lock():
            session = self.get(task_id)
            prior = self._existing(session, "hypothesis", key, digest)
            if prior is not None:
                return self._reconcile_phase_block(
                    session,
                    expected_event_hash=prior.event_hashes[-1],
                )
            phase = self._require_running_workflow_phase(session, "hypothesis")
            session = self._ensure_executing(session)
            if session.unresolved_intents:
                raise _error("task_blocked", "unresolved intent blocks hypotheses")
            if phase is None or phase.phase_id != request["phase_id"]:
                raise _error(
                    "phase_mismatch",
                    "phase_id does not match the running workflow phase",
                )
            if (phase.playbook, phase.name) not in {
                ("root-cause-protocol", "hypothesis"),
                ("debugging", "isolate"),
            }:
                raise _error(
                    "hypothesis_not_allowed",
                    "hypotheses are only allowed in a debugging hypothesis phase",
                )
            evidence = self._phase_evidence(session, phase, refs, "failed")
            hypothesis_count = session.hypothesis_count + int(status == "rejected")
            failed_hypotheses = self._failed_hypotheses(session) + int(
                status == "rejected"
            )
            operation = self._append_operation(
                session,
                "hypothesis",
                key,
                digest,
                request,
                {
                    "phase_id": phase.phase_id,
                    "status": status,
                    "evidence_refs": list(evidence),
                    "hypothesis_count": hypothesis_count,
                    "failed_hypotheses": failed_hypotheses,
                },
            )
            session = self.get(task_id)
            return self._reconcile_phase_block(
                session,
                expected_event_hash=operation.entry_hash,
            )

    def approve(
        self,
        task_id: str,
        *,
        stage: str,
        approved: bool,
        approver: str,
        rationale: str,
        phase_id: str | None = None,
        evidence_refs: tuple[str, ...] = (),
        idempotency_key: str,
        proof: str | None = None,
    ) -> TaskSession:
        key = _idempotency_key(idempotency_key)
        action_stage = stage in IRREVERSIBLE_ACTIONS
        if stage not in {"plan", "final", "phase"} and not action_stage:
            raise _error(
                "validation_error",
                "approval stage must be plan, final, phase, or an irreversible action kind",
            )
        if stage == "phase":
            if not isinstance(phase_id, str) or not phase_id.strip():
                raise _error("validation_error", "phase approval requires phase_id")
            phase_id = phase_id.strip()
        elif phase_id is not None:
            raise _error("validation_error", "phase_id is only valid for phase approval")
        if not isinstance(approved, bool):
            raise _error("validation_error", "approved must be a boolean")
        if not isinstance(approver, str) or not approver.strip():
            raise _error("validation_error", "approver must be non-blank")
        if not isinstance(rationale, str) or not rationale.strip():
            raise _error("validation_error", "rationale must be non-blank")
        refs = tuple(evidence_refs)
        request = {
            "stage": stage,
            "approved": approved,
            "approver": approver.strip(),
            "rationale": rationale.strip(),
            "evidence_refs": list(refs),
        }
        if stage == "phase":
            request["phase_id"] = phase_id
        digest = canonical_sha256(request)
        with self.runtime.execution_lock():
            session = self.get(task_id)
            prior = self._existing(session, "approve", key, digest)
            if prior is not None:
                if stage == "phase":
                    current = next(
                        (
                            phase
                            for phase in session.workflow_phases
                            if phase.phase_id == session.current_phase_id
                        ),
                        None,
                    )
                    if (
                        approved
                        and session.state is TaskState.BLOCKED
                        and current is not None
                        and current.status == "failed"
                    ):
                        self._append_transition(
                            session,
                            TaskState.EXECUTING,
                            reason="trusted phase approval restarted debugging",
                            cause_event_hash=prior.event_hashes[-1],
                        )
                        return self.get(task_id)
                    return session
                if not approved:
                    if action_stage:
                        return session
                    if session.state is TaskState.REJECTED:
                        return session
                    stored_state = self._durable_state(task_id)
                    if stored_state is not TaskState.REJECTED:
                        self._append_transition(
                            replace(session, state=stored_state),
                            TaskState.REJECTED,
                            reason=f"trusted {stage} rejection recorded",
                            cause_event_hash=prior.event_hashes[-1],
                        )
                        return self.get(task_id)
                    return session
                if (
                    stage == "plan"
                    and approved
                    and session.state is TaskState.PLANNED
                ):
                    self._append_transition(
                        session,
                        TaskState.APPROVED,
                        reason="trusted plan approval recorded",
                        cause_event_hash=prior.event_hashes[-1],
                    )
                    return self.get(task_id)
                if (
                    stage == "final"
                    and approved
                    and session.state is TaskState.BLOCKED
                    and not session.unresolved_intents
                ):
                    self._append_transition(
                        session,
                        TaskState.EXECUTING,
                        reason="trusted final approval resolved completion escalation",
                        cause_event_hash=prior.event_hashes[-1],
                    )
                    return self.get(task_id)
                return session

            decision = self._authoritative_event(
                task_id,
                "approve",
                key,
                digest,
                AuditEventType.HUMAN_DECISION,
            )
            if decision is None:
                if session.terminal:
                    raise _error("task_terminal", "terminal task cannot be approved")
                if stage == "plan" and session.state is not TaskState.PLANNED:
                    raise _error("invalid_transition", "plan decision requires planned state")
                if stage == "final" and session.state not in {
                    TaskState.EXECUTING,
                    TaskState.BLOCKED,
                }:
                    raise _error(
                        "invalid_transition",
                        "final decision requires executing or completion-blocked state",
                    )
                if stage == "phase":
                    if self._durable_state(task_id) is not TaskState.BLOCKED:
                        raise _error(
                            "recovery_required",
                            "replay the exact blocked phase operation before approval",
                            retryable=True,
                        )
                    current = next(
                        (
                            phase
                            for phase in session.workflow_phases
                            if phase.phase_id == session.current_phase_id
                        ),
                        None,
                    )
                    if (
                        session.state is not TaskState.BLOCKED
                        or current is None
                        or current.status != "blocked"
                        or current.phase_id != phase_id
                    ):
                        raise _error(
                            "invalid_transition",
                            "phase decision requires the matching blocked phase",
                        )
                    if not refs:
                        raise _error(
                            "evidence_scope_mismatch",
                            "phase approval requires task evidence",
                        )
                if (
                    stage == "final"
                    and session.state is TaskState.BLOCKED
                    and session.unresolved_intents
                ):
                    raise _error(
                        "unresolved_action_intent",
                        "final approval cannot resolve an uncertain action",
                    )
                self._authorize(approver.strip(), stage, proof)
                self._validate_evidence_refs(task_id, refs)
                if stage == "phase" and set(refs) != set(
                    self._phase_approval_evidence(session)
                ):
                    raise _error(
                        "evidence_scope_mismatch",
                        "phase approval must cite the current rejection streak and gate",
                    )
                if stage == "final" and approved and set(refs) != set(
                    self._current_evidence(session)
                ):
                    raise _error(
                        "evidence_scope_mismatch",
                        "final approval must cite the exact current evidence set",
                    )
                metadata = self._operation_metadata(
                    task_id,
                    "approve",
                    key,
                    digest,
                )
                if approved:
                    decision = self.runtime.approve(
                        self._contract(session),
                        stage,
                        approver.strip(),
                        rationale.strip(),
                        evidence_refs=refs,
                        metadata=metadata,
                    )
                else:
                    decision = self.runtime.reject(
                        self._contract(session),
                        stage,
                        approver.strip(),
                        rationale.strip(),
                        metadata=metadata,
                    )
            if stage == "phase":
                operation = self._append_operation(
                    session,
                    "approve",
                    key,
                    digest,
                    request,
                    {
                        "stage": stage,
                        "approved": approved,
                        "phase_id": phase_id,
                        "decision_event_hash": decision.entry_hash,
                    },
                )
                if approved:
                    blocked = self.get(task_id)
                    self._append_transition(
                        blocked,
                        TaskState.EXECUTING,
                        reason="trusted phase approval restarted debugging",
                        cause_event_hash=operation.entry_hash,
                    )
                return self.get(task_id)
            if not approved and not action_stage:
                self._append_transition(
                    session,
                    TaskState.REJECTED,
                    reason=f"trusted {stage} rejection recorded",
                    cause_event_hash=decision.entry_hash,
                )
                return self.get(task_id)
            if stage == "final" and session.state is TaskState.BLOCKED:
                operation = self._append_operation(
                    session,
                    "approve",
                    key,
                    digest,
                    request,
                    {
                        "stage": stage,
                        "approved": True,
                        "decision_event_hash": decision.entry_hash,
                    },
                )
                self._append_transition(
                    session,
                    TaskState.EXECUTING,
                    reason="trusted final approval resolved completion escalation",
                    cause_event_hash=operation.entry_hash,
                )
                return self.get(task_id)
            if stage == "plan" and session.state is TaskState.PLANNED:
                self._append_transition(
                    session,
                    TaskState.APPROVED,
                    reason="trusted plan approval recorded",
                    cause_event_hash=decision.entry_hash,
                )
                session = self.get(task_id)
            self._append_operation(
                session,
                "approve",
                key,
                digest,
                request,
                {
                    "stage": stage,
                    "approved": approved,
                    "decision_event_hash": decision.entry_hash,
                },
            )
            return self.get(task_id)

    def verify(
        self,
        task_id: str,
        requirement_id: str,
        *,
        idempotency_key: str,
        mode: str = "execute",
        evidence_hash: str | None = None,
        approved: bool | None = None,
        approver: str | None = None,
        rationale: str | None = None,
        proof: str | None = None,
    ) -> TaskSession:
        key = _idempotency_key(idempotency_key)
        if not isinstance(requirement_id, str) or not requirement_id.strip():
            raise _error("validation_error", "requirement_id must be non-blank")
        if mode not in {"execute", "manual"}:
            raise _error("validation_error", "verify mode must be execute or manual")
        request: dict[str, Any] = {
            "requirement_id": requirement_id.strip(),
            "mode": mode,
        }
        if mode == "manual":
            request.update(
                {
                    "evidence_hash": evidence_hash,
                    "approved": approved,
                    "approver": approver,
                    "rationale": rationale,
                }
            )
        digest = canonical_sha256(request)
        with self.runtime.execution_lock():
            session = self.get(task_id)
            prior = self._existing(session, "verify", key, digest)
            if prior is not None:
                response = prior.response if isinstance(prior.response, Mapping) else {}
                evidence_hash_value = response.get("event_hash")
                evidence_event = next(
                    (
                        event
                        for event in self.ledger.events_for_contract(
                            task_id, all_segments=True
                        )
                        if event.entry_hash == evidence_hash_value
                    ),
                    None,
                )
                must_block = response.get("status") in {
                    "blocked",
                    "timeout",
                    "error",
                } or bool(
                    evidence_event
                    and evidence_event.payload.get("mutates_task") is True
                )
                if must_block:
                    if (
                        session.workflow_phases
                        and self._durable_state(task_id) is TaskState.EXECUTING
                    ):
                        return self._reconcile_phase_block(
                            session,
                            expected_event_hash=prior.event_hashes[-1],
                        )
                    if (
                        not session.workflow_phases
                        and session.state is TaskState.EXECUTING
                    ):
                        self._append_transition(
                            session,
                            TaskState.BLOCKED,
                            reason=f"verification ended with {response.get('status')}",
                            cause_event_hash=prior.event_hashes[-1],
                        )
                        return self.get(task_id)
                return session
            self._require_running_workflow_phase(session, "verification")
            session = self._ensure_executing(session)
            contract = self._contract(session)
            requirement = next(
                (
                    item
                    for item in contract.verification_requirements
                    if item.id == requirement_id.strip()
                ),
                None,
            )
            if requirement is None:
                raise _error("requirement_not_found", "verification requirement not found")
            if requirement.manual != (mode == "manual"):
                raise _error("requirement_mode_mismatch", "verification mode differs from contract")
            if not requirement.manual and not self.policy.allows_verification(
                requirement.argv
            ):
                raise _error(
                    "policy_denied",
                    "verification argv is not in the current server allowlist",
                )
            operation_id = self._operation_id(task_id, "verify", key, digest)
            if mode == "manual":
                if (
                    not isinstance(evidence_hash, str)
                    or evidence_hash not in self._scoped_hashes(task_id)
                    or not isinstance(approved, bool)
                    or not isinstance(approver, str)
                    or not approver.strip()
                    or not isinstance(rationale, str)
                    or not rationale.strip()
                ):
                    raise _error("validation_error", "manual verification fields are invalid")
                decision = self._authoritative_event(
                    task_id,
                    "verify",
                    key,
                    digest,
                    AuditEventType.HUMAN_DECISION,
                )
                if decision is None:
                    self._authorize(approver.strip(), "verification", proof)
                    decision = self.runtime.record_manual_verification(
                        contract,
                        requirement.id,
                        evidence_hash=evidence_hash,
                        approved=approved,
                        approver=approver.strip(),
                        rationale=rationale.strip(),
                        metadata=self._operation_metadata(
                            task_id,
                            "verify",
                            key,
                            digest,
                        ),
                    )
                self._append_operation(
                    session,
                    "verify",
                    key,
                    digest,
                    request,
                    {
                        "requirement_id": requirement.id,
                        "status": "pass" if approved else "fail",
                        "evidence_hash": evidence_hash,
                        "decision_hash": decision.entry_hash,
                    },
                )
                return self.get(task_id)

            intent: LedgerEvent | None = None
            descriptor = {"kind": "verify", "requirement_id": requirement.id}

            def before_effect() -> None:
                nonlocal intent
                intent = self.ledger.append(
                    AuditEventType.TASK_ACTION_INTENT,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "task_id": task_id,
                        "operation": "verify",
                        "operation_id": operation_id,
                        "idempotency_key": key,
                        "request_sha256": digest,
                        "descriptor": descriptor,
                    },
                    contract_id=task_id,
                )

            result = self.runtime.verify_requirement(
                contract,
                requirement.id,
                before_effect=before_effect,
                transition_on_failure=False,
            )
            if intent is None:
                raise RuntimeError("verification completed without durable intent")
            result_event = self.ledger.append(
                AuditEventType.TASK_ACTION_RESULT,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "operation": "verify",
                    "operation_id": operation_id,
                    "idempotency_key": key,
                    "request_sha256": digest,
                    "descriptor": descriptor,
                    "outcome": "completed",
                    "result": result.to_dict(),
                    "response": result.to_dict(),
                    "provenance_event_hashes": [result.event_hash],
                },
                contract_id=task_id,
            )
            evidence_event = next(
                (
                    event
                    for event in self.ledger.events_for_contract(task_id, all_segments=True)
                    if event.entry_hash == result.event_hash
                ),
                None,
            )
            unsafe = bool(evidence_event and evidence_event.payload.get("mutates_task") is True)
            if result.status in {"blocked", "timeout", "error"} or unsafe:
                blocked = self.get(task_id)
                if blocked.workflow_phases:
                    return self._reconcile_phase_block(
                        blocked,
                        expected_event_hash=result_event.entry_hash,
                    )
                self._append_transition(
                    blocked,
                    TaskState.BLOCKED,
                    reason=f"verification ended with {result.status}",
                    cause_event_hash=result_event.entry_hash,
                )
            return self.get(task_id)

    def verdict(
        self,
        task_id: str,
        *,
        verifier: str,
        status: str,
        rationale: str,
        severity: str = "normal",
        evidence_refs: tuple[str, ...] = (),
        idempotency_key: str,
    ) -> TaskSession:
        key = _idempotency_key(idempotency_key)
        if status not in {"pass", "fail"} or severity not in {"normal", "critical"}:
            raise _error("validation_error", "invalid verifier status or severity")
        if not isinstance(verifier, str) or not verifier.strip():
            raise _error("validation_error", "verifier must be non-blank")
        if not isinstance(rationale, str) or not rationale.strip():
            raise _error("validation_error", "verifier rationale must be non-blank")
        refs = tuple(evidence_refs)
        request = {
            "verifier": verifier.strip(),
            "status": status,
            "rationale": rationale.strip(),
            "severity": severity,
            "evidence_refs": list(refs),
        }
        digest = canonical_sha256(request)
        with self.runtime.execution_lock():
            session = self.get(task_id)
            if self._existing(session, "verdict", key, digest) is not None:
                return session
            self._require_running_workflow_phase(session, "verdict")
            session = self._ensure_executing(session)
            self._validate_evidence_refs(task_id, refs)
            decision = VerifierDecision(
                verifier.strip(),
                status,
                rationale.strip(),
                severity=severity,
                evidence_refs=refs,
            )
            event = self._authoritative_event(
                task_id,
                "verdict",
                key,
                digest,
                AuditEventType.VERIFIER_DECISION,
            )
            if event is None:
                current_refs = self._current_evidence(session)
                if status == "pass" and set(refs) != set(current_refs):
                    raise _error(
                        "evidence_scope_mismatch",
                        "pass verdict must cite the exact current evidence set",
                    )
                event = self.runtime.record_verifier(
                    self._contract(session),
                    decision,
                    metadata=self._operation_metadata(
                        task_id,
                        "verdict",
                        key,
                        digest,
                    ),
                )
            else:
                current_refs = refs
            self._append_operation(
                session,
                "verdict",
                key,
                digest,
                request,
                {
                    "decision": decision.to_dict(),
                    "decision_event_hash": event.entry_hash,
                    "current_evidence_refs": list(current_refs),
                },
            )
            return self.get(task_id)

    def complete(self, task_id: str, *, idempotency_key: str) -> TaskSession:
        key = _idempotency_key(idempotency_key)
        request: dict[str, Any] = {}
        digest = canonical_sha256(request)
        with self.runtime.execution_lock():
            session = self.get(task_id)
            prior = self._existing(session, "complete", key, digest)
            if prior is not None:
                response = prior.response
                if not isinstance(response, Mapping):
                    raise _error(
                        "invalid_task_event",
                        "complete operation response must be an object",
                    )
                gate_response = response.get("gate")
                gate_event_hash = response.get("gate_event_hash")
                if (
                    not isinstance(gate_response, Mapping)
                    or not isinstance(gate_event_hash, str)
                ):
                    raise _error(
                        "invalid_task_event",
                        "complete operation must cite its gate response",
                    )
                gate_value = gate_response.get("decision")
                if (
                    gate_value
                    in {GateDecision.ESCALATE.value, GateDecision.STOP.value}
                    and session.state is TaskState.BLOCKED
                    and any(
                        intent.kind == "completion"
                        and intent.operation_id == prior.operation_id
                        for intent in session.unresolved_intents
                    )
                ):
                    stored_state = self._durable_state(task_id)
                    if stored_state is not TaskState.BLOCKED:
                        self._append_transition(
                            replace(session, state=stored_state),
                            TaskState.BLOCKED,
                            reason="completion gate requires intervention",
                            cause_event_hash=prior.event_hashes[-1],
                        )
                        return self.get(task_id)
                    return session
                if session.state is not TaskState.EXECUTING:
                    return session
                self._assert_completion_snapshot_current(
                    task_id,
                    gate_event_hash,
                    operation_event_hash=prior.event_hashes[-1],
                )
                if gate_value == GateDecision.PASS.value:
                    self._append_transition(
                        session,
                        TaskState.VERIFIED,
                        reason="completion gate passed",
                        cause_event_hash=prior.event_hashes[-1],
                    )
                    return self.get(task_id)
                if (
                    gate_value in {GateDecision.ESCALATE.value, GateDecision.STOP.value}
                ):
                    self._append_transition(
                        session,
                        TaskState.BLOCKED,
                        reason="completion gate requires intervention",
                        cause_event_hash=prior.event_hashes[-1],
                    )
                    return self.get(task_id)
                return session
            if session.terminal:
                raise _error("task_terminal", "terminal task cannot complete again")
            incomplete = [
                phase.phase_id
                for phase in session.workflow_phases
                if phase.status != "passed"
            ]
            if incomplete:
                raise _error(
                    "workflow_incomplete",
                    "all workflow phases must pass before completion",
                    phases=incomplete,
                )
            gate_event = self._authoritative_event(
                task_id,
                "complete",
                key,
                digest,
                AuditEventType.GATE_DECISION,
            )
            if gate_event is None:
                session = self._ensure_executing(session)
                if session.unresolved_intents:
                    raise _error("task_blocked", "unresolved intent blocks completion")
                metadata = self._operation_metadata(
                    task_id,
                    "complete",
                    key,
                    digest,
                )
                metadata["completion_workspace_fingerprint_sha256"] = (
                    self._workspace_fingerprint_digest()
                )
                gate = self.runtime.complete(
                    self._contract(session),
                    min_passes=2,
                    event_metadata=metadata,
                )
                gate_event = next(
                    event
                    for event in reversed(
                        self.ledger.events_for_contract(task_id, all_segments=True)
                    )
                    if event.event_type == AuditEventType.GATE_DECISION.value
                    and event.payload.get("operation") == "complete"
                    and event.payload.get("idempotency_key") == key
                )
            else:
                matching_completion = tuple(
                    intent
                    for intent in session.unresolved_intents
                    if intent.kind == "completion"
                    and intent.operation_id
                    == gate_event.payload.get("operation_id")
                )
                if session.unresolved_intents and len(matching_completion) != len(
                    session.unresolved_intents
                ):
                    raise _error("task_blocked", "another unresolved intent blocks completion")
                gate = GateResult(
                    GateDecision(gate_event.payload["decision"]),
                    tuple(gate_event.payload.get("reasons", ())),
                )
            self._assert_completion_snapshot_current(
                task_id,
                gate_event.entry_hash,
            )
            operation = self._append_operation(
                session,
                "complete",
                key,
                digest,
                request,
                {"gate": gate.to_dict(), "gate_event_hash": gate_event.entry_hash},
            )
            session = self.get(task_id)
            durable_state = self._durable_state(task_id)
            transition_session = replace(session, state=durable_state)
            self._assert_completion_snapshot_current(
                    task_id,
                gate_event.entry_hash,
                operation_event_hash=operation.entry_hash,
            )
            if gate.decision is GateDecision.PASS:
                self._append_transition(
                    transition_session,
                    TaskState.VERIFIED,
                    reason="completion gate passed",
                    cause_event_hash=operation.entry_hash,
                )
            elif gate.decision in {GateDecision.ESCALATE, GateDecision.STOP}:
                if durable_state is not TaskState.BLOCKED:
                    self._append_transition(
                        transition_session,
                        TaskState.BLOCKED,
                        reason="completion gate requires intervention",
                        cause_event_hash=operation.entry_hash,
                    )
            return self.get(task_id)

    def append_evidence(
        self,
        task_id: str,
        kind: str,
        payload: Mapping[str, Any],
        *,
        artifact_paths: tuple[str | Path, ...] = (),
        idempotency_key: str,
    ) -> TaskSession:
        key = _idempotency_key(idempotency_key)
        safe_kinds = {
            EvidenceKind.TEST_OUTPUT.value,
            EvidenceKind.BROWSER_DIFF.value,
            EvidenceKind.ARTIFACT_HASH.value,
            EvidenceKind.TOOL_OUTPUT.value,
            EvidenceKind.A11Y_REPORT.value,
        }
        if kind not in safe_kinds or not isinstance(payload, Mapping):
            raise _error("evidence_kind_not_declared", "evidence kind is not MCP-safe")
        allowed_payload = {"summary", "status", "details"}
        if set(payload) - allowed_payload:
            raise _error(
                "validation_error",
                "evidence payload contains reserved or unknown fields",
                fields=sorted(set(payload) - allowed_payload),
            )
        summary = payload.get("summary")
        status = payload.get("status")
        details = payload.get("details", {})
        if not isinstance(summary, str) or not summary.strip():
            raise _error("validation_error", "evidence summary must be non-blank")
        if status not in {"pass", "fail", "info"}:
            raise _error("validation_error", "evidence status must be pass, fail, or info")
        if not isinstance(details, Mapping):
            raise _error("validation_error", "evidence details must be an object")
        safe_payload = {
            "summary": summary.strip(),
            "status": status,
            "details": dict(details),
            "mutates_task": False,
        }
        request = {
            "kind": kind,
            "payload": safe_payload,
            "artifact_paths": [str(item) for item in artifact_paths],
        }
        digest = canonical_sha256(request)
        with self.runtime.execution_lock():
            session = self.get(task_id)
            if self._existing(session, "append_evidence", key, digest) is not None:
                return session
            self._require_running_workflow_phase(session, "evidence")
            session = self._ensure_executing(session)
            contract = self._contract(session)
            declared = {item.kind_value for item in contract.evidence_required}
            if kind not in declared:
                raise _error(
                    "evidence_kind_not_declared",
                    "evidence kind is not declared by the frozen contract",
                )
            resolved = tuple(
                self._resolve_project_path(path, field_name="artifact_path")
                for path in artifact_paths
            )
            event = self._authoritative_event(
                task_id,
                "append_evidence",
                key,
                digest,
                AuditEventType.EVIDENCE,
            )
            if event is None:
                event = self.runtime.record_evidence(
                    contract,
                    kind,
                    {
                        **safe_payload,
                        **self._operation_metadata(
                            task_id,
                            "append_evidence",
                            key,
                            digest,
                        ),
                    },
                    artifact_paths=resolved,
                )
            self._append_operation(
                session,
                "append_evidence",
                key,
                digest,
                request,
                {"kind": kind, "evidence_hash": event.entry_hash},
            )
            return self.get(task_id)

    def reflect(
        self,
        task_id: str,
        *,
        idempotency_key: str,
        failure_scope: str | None = None,
        failure_ttl_days: int | None = None,
    ) -> TaskSession:
        key = _idempotency_key(idempotency_key)
        if failure_ttl_days is not None and (
            isinstance(failure_ttl_days, bool)
            or not isinstance(failure_ttl_days, int)
            or failure_ttl_days < 1
        ):
            raise _error("validation_error", "failure_ttl_days must be a positive integer")
        request = {
            "failure_scope": failure_scope,
            "failure_ttl_days": failure_ttl_days,
        }
        digest = canonical_sha256(request)
        with self.runtime.execution_lock():
            session = self.get(task_id)
            intents = [
                event
                for event in self.ledger.events_for_contract(task_id, all_segments=True)
                if event.event_type == TASK_REFLECTION_INTENT
            ]
            if session.reflection is not None:
                intent = intents[-1] if intents else None
                if (
                    intent is None
                    or intent.payload.get("idempotency_key") != key
                    or intent.payload.get("request_sha256") != digest
                ):
                    raise _error("reflection_conflict", "task was reflected with other options")
                return session
            if not session.terminal:
                raise _error("invalid_transition", "only terminal tasks may reflect")
            pending = next(
                (item for item in session.unresolved_intents if item.kind == "reflection"),
                None,
            )
            if pending is not None:
                if pending.idempotency_key != key or pending.request_sha256 != digest:
                    raise _error("reflection_conflict", "reflection is already in progress")
                reflection_id = pending.operation_id
                source_hash = str(pending.descriptor["source_event_hash"])
                intent_event = next(
                    event for event in intents if event.payload.get("reflection_id") == reflection_id
                )
                created_at = str(intent_event.payload["created_at"])
            else:
                terminal_events = [
                    event
                    for event in self.ledger.events_for_contract(task_id, all_segments=True)
                    if event.event_type == STATE_TRANSITION
                    and event.payload.get("state")
                    in {TaskState.VERIFIED.value, TaskState.REJECTED.value}
                ]
                if not terminal_events:
                    terminal_events = [
                        event
                        for event in self.ledger.events_for_contract(
                            task_id, all_segments=True
                        )
                        if _is_correlated_approval_rejection(event, task_id)
                    ]
                if not terminal_events:
                    raise _error(
                        "invalid_transition",
                        "terminal task has no durable terminal transition",
                    )
                source_hash = terminal_events[-1].entry_hash
                reflection_id = sha256_text(f"{task_id}:{source_hash}:reflection:v1")
                created_at = utc_now()
                self.ledger.append(
                    AuditEventType.TASK_REFLECTION_INTENT,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "task_id": task_id,
                        "reflection_id": reflection_id,
                        "source_event_hash": source_hash,
                        "created_at": created_at,
                        "idempotency_key": key,
                        "request_sha256": digest,
                    },
                    contract_id=task_id,
                )
            reflection = reflect_on_contract(
                self.ledger,
                TypedMemory(self.project_root),
                self._contract(session),
                failure_scope=failure_scope,
                failure_ttl_days=failure_ttl_days,
                reflection_id=reflection_id,
                created_at=created_at,
                source_event_hash=source_hash,
            )
            response = reflection.to_dict()
            self.ledger.append(
                AuditEventType.TASK_REFLECTED,
                {
                    "schema_version": SCHEMA_VERSION,
                    "task_id": task_id,
                    "reflection_id": reflection_id,
                    "source_event_hash": source_hash,
                    "memory_entry_ids": [
                        response["retrospective"]["entry_id"],
                        *[item["entry_id"] for item in response["failures"]],
                    ],
                    "response": response,
                },
                contract_id=task_id,
            )
            return self.get(task_id)

    def get(self, task_id: str, recover: bool = True) -> TaskSession:
        """Return the durable projection; recovery is derivation-only.

        ``recover`` is retained for the write-capable service layer.  This
        projection never appends: both modes report an orphan action intent as
        blocked, while the caller decides whether to record a trusted recovery.
        """

        if not isinstance(recover, bool):
            raise _error("invalid_request", "recover must be a boolean")
        if not isinstance(task_id, str) or not task_id.strip():
            raise _error("invalid_task_id", "task_id must be a non-blank string")
        task_id = task_id.strip()

        # Verification and the all-segment snapshot must be atomic with respect
        # to append/rotation; file_lock is re-entrant for verify_chain().
        with file_lock(self.ledger.path):
            if not self.ledger.verify_chain():
                raise _error(
                    "ledger_integrity_failed",
                    "ledger hash chain verification failed",
                    retryable=False,
                )
            events = self.ledger.events(all_segments=True)

        return self._project(task_id, events)

    def _project(self, task_id: str, events: list[LedgerEvent]) -> TaskSession:
        # Detect both directions of a cross-task splice instead of merely
        # filtering it away.
        scoped: list[LedgerEvent] = []
        for event in events:
            payload_task = event.payload.get("task_id") if isinstance(event.payload, Mapping) else None
            relevant_type = event.event_type in _TASK_EVENTS
            if relevant_type and (event.contract_id == task_id or payload_task == task_id):
                if event.contract_id != task_id or payload_task != task_id:
                    raise _error(
                        "task_identity_mismatch",
                        "task event crosses contract scope",
                        event_hash=event.entry_hash,
                        task_id=task_id,
                        payload_task_id=payload_task,
                        contract_id=event.contract_id,
                    )
            if event.contract_id == task_id:
                scoped.append(event)

        contracts = [event for event in scoped if event.event_type == GOAL_CONTRACT]
        starts = [event for event in scoped if event.event_type == TASK_STARTED]
        if not contracts and not starts:
            raise _error("task_not_found", f"task not found: {task_id}")
        if len(contracts) != 1 or len(starts) != 1:
            raise _error(
                "invalid_task_cardinality",
                "task requires exactly one goal_contract and one task_started event",
                task_id=task_id,
                goal_contract_count=len(contracts),
                task_started_count=len(starts),
            )

        contract_event = contracts[0]
        start_event = starts[0]
        if scoped.index(contract_event) > scoped.index(start_event):
            raise _error(
                "invalid_task_order",
                "goal_contract must precede task_started",
                task_id=task_id,
            )
        contract = contract_event.payload
        if contract.get("goal_id") != task_id:
            raise _error(
                "task_identity_mismatch",
                "goal_contract.goal_id differs from task_id",
                task_id=task_id,
                goal_id=contract.get("goal_id"),
            )
        try:
            initial_state = TaskState(contract.get("state", TaskState.PLANNED.value))
        except (TypeError, ValueError) as exc:
            raise _error(
                "invalid_task_contract",
                "goal_contract.state is not a task state",
                state=contract.get("state"),
            ) from exc
        if initial_state is not TaskState.PLANNED:
            raise _error(
                "invalid_task_contract",
                "task lifecycle contracts must start in planned state",
                state=initial_state.value,
            )

        started = _common_payload(start_event, task_id)
        begin_key = _required_text(started, "idempotency_key", start_event)
        begin_digest = _request_digest(started, start_event)
        begin_response = _response(started, start_event)
        workflow = started.get("workflow", "legacy")
        if workflow not in {"legacy", "auto", "root-cause-protocol"}:
            raise _error(
                "invalid_task_event",
                "task_started.workflow is invalid",
                event_hash=start_event.entry_hash,
            )
        phase_plan_value = started.get("phase_plan", ())
        workflow_phases = _initial_workflow_phases(phase_plan_value)
        begin_request = started.get("request")
        if not isinstance(begin_request, Mapping) or canonical_sha256(
            _thaw(begin_request)
        ) != begin_digest:
            raise _error(
                "invalid_task_event",
                "task_started request does not match request_sha256",
                event_hash=start_event.entry_hash,
            )
        if (
            not isinstance(begin_response, Mapping)
            or begin_response.get("task_id") != task_id
            or begin_response.get("contract_id") != task_id
        ):
            raise _error(
                "invalid_task_event",
                "task_started response identity is invalid",
                event_hash=start_event.entry_hash,
            )
        if workflow == "legacy":
            valid_workflow = not workflow_phases
        else:
            expected_plan = tuple(_thaw(phase_plan_value))
            expected_request = _workflow_begin_request(
                GoalContract.from_mapping(contract),
                workflow,
                expected_plan,
            )
            expected_response = {
                "task_id": task_id,
                "contract_id": task_id,
                "workflow": workflow,
                "phase_plan": list(expected_plan),
            }
            valid_workflow = bool(
                (workflow == "auto" or workflow_phases)
                and _thaw(begin_request) == expected_request
                and _thaw(begin_response) == expected_response
            )
        if not valid_workflow:
            raise _error(
                "invalid_task_event",
                "task_started workflow and phase plan disagree",
                event_hash=start_event.entry_hash,
            )

        idempotency: dict[tuple[str, str], IdempotencyRecord] = {
            ("begin", begin_key): IdempotencyRecord(
                operation="begin",
                idempotency_key=begin_key,
                request_sha256=begin_digest,
                operation_id=task_id,
                request=_freeze(started.get("request")),
                response=begin_response,
                outcome="completed",
                event_hashes=(start_event.entry_hash,),
            )
        }
        operation_ids: dict[str, tuple[str, str]] = {task_id: ("begin", begin_key)}
        pending: dict[str, PendingIntent] = {}
        completion_intents: dict[str, PendingIntent] = {}
        completion_blocks: dict[str, PendingIntent] = {}
        authoritative_rejections: dict[str, LedgerEvent] = {}
        requirement_results: dict[str, Any] = {}
        for requirement in contract.get("verification_requirements", ()):
            if isinstance(requirement, Mapping):
                requirement_id = requirement.get("id")
                if isinstance(requirement_id, str) and requirement_id:
                    requirement_results[requirement_id] = _freeze({"status": "pending"})
        try:
            max_failed_hypotheses = int(
                contract.get("stopping_policy", {}).get(
                    "max_failed_hypotheses",
                    0,
                )
                or 0
            )
        except (TypeError, ValueError) as exc:
            raise _error(
                "invalid_task_contract",
                "max_failed_hypotheses must be an integer",
            ) from exc
        if max_failed_hypotheses < 0:
            raise _error(
                "invalid_task_contract",
                "max_failed_hypotheses cannot be negative",
            )
        hypothesis_count = 0
        failed_hypotheses = 0

        state = initial_state
        terminal_hash: str | None = None
        reflection: Mapping[str, Any] | None = None
        reflection_intent: PendingIntent | None = None
        seen_positions: dict[str, int] = {}
        seen_events: dict[str, LedgerEvent] = {}
        rejected_hypothesis_hashes: list[str] = []
        hypothesis_gate_hash: str | None = None
        phase_block_hash: str | None = None
        started_seen = False

        for event in scoped:
            seen_positions[event.entry_hash] = len(seen_positions)
            seen_events[event.entry_hash] = event
            if event is start_event:
                started_seen = True
                continue
            if event is contract_event:
                continue
            if not started_seen and (
                event.event_type in _TASK_EVENTS or event.event_type == STATE_TRANSITION
            ):
                raise _error(
                    "invalid_task_order",
                    "task lifecycle event precedes task_started",
                    event_hash=event.entry_hash,
                )

            if any(
                phase["status"] == "blocked" for phase in workflow_phases
            ) and (
                event.payload.get("mutates_task") is True
                or event.event_type == TASK_ACTION_INTENT
            ):
                raise _error(
                    "invalid_task_state",
                    "blocked workflow phase cannot record new effects",
                    event_hash=event.entry_hash,
                )

            if event.event_type == STATE_TRANSITION:
                payload = event.payload
                if payload.get("task_id") != task_id:
                    raise _error(
                        "task_identity_mismatch",
                        "state transition lacks the matching task_id",
                        event_hash=event.entry_hash,
                    )
                source = _state(payload.get("from_state"), event, "from_state")
                target = _state(payload.get("state"), event, "state")
                cause = _required_text(payload, "cause_event_hash", event)
                if source is not state:
                    raise _error(
                        "invalid_task_transition",
                        "state transition from_state differs from projected state",
                        event_hash=event.entry_hash,
                        expected=state.value,
                        actual=source.value,
                    )
                if target not in _TRANSITIONS[source]:
                    raise _error(
                        "invalid_task_transition",
                        f"illegal task state edge {source.value}->{target.value}",
                        event_hash=event.entry_hash,
                    )
                if cause == event.entry_hash or cause not in seen_events:
                    raise _error(
                        "invalid_task_transition",
                        "cause_event_hash does not cite an earlier task event",
                        event_hash=event.entry_hash,
                        cause_event_hash=cause,
                    )
                state = target
                if target is TaskState.BLOCKED:
                    completed_id = next(
                        (
                            operation_id
                            for operation_id, intent in completion_blocks.items()
                            if intent.event_hash == cause
                        ),
                        None,
                    )
                    if completed_id is not None:
                        completion_blocks.pop(completed_id)
                if state in {TaskState.VERIFIED, TaskState.REJECTED}:
                    terminal_hash = event.entry_hash
                continue

            if (
                event.event_type == AuditEventType.GATE_DECISION.value
                and event.payload.get("operation") == "hypothesis"
            ):
                payload = _common_payload(event, task_id)
                key = _required_text(payload, "idempotency_key", event)
                digest = _request_digest(payload, event)
                operation_id = _required_text(payload, "operation_id", event)
                record = idempotency.get(("hypothesis", key))
                if (
                    max_failed_hypotheses < 1
                    or hypothesis_gate_hash is not None
                    or payload.get("decision") != GateDecision.ESCALATE.value
                    or record is None
                    or record.operation_id != operation_id
                    or record.request_sha256 != digest
                    or not isinstance(record.response, Mapping)
                    or record.response.get("status") != "rejected"
                    or record.event_hashes[-1]
                    not in rejected_hypothesis_hashes
                    or record.response.get("failed_hypotheses")
                    != len(rejected_hypothesis_hashes)
                    or record.response.get("failed_hypotheses", 0)
                    < max_failed_hypotheses
                ):
                    raise _error(
                        "invalid_task_event",
                        "hypothesis escalation gate is invalid",
                        event_hash=event.entry_hash,
                    )
                hypothesis_gate_hash = event.entry_hash
                continue

            if (
                event.event_type == AuditEventType.GATE_DECISION.value
                and event.payload.get("operation") == "complete"
                and event.payload.get("decision")
                in {GateDecision.ESCALATE.value, GateDecision.STOP.value}
            ):
                payload = _common_payload(event, task_id)
                operation_id = _required_text(payload, "operation_id", event)
                key = _required_text(payload, "idempotency_key", event)
                digest = _request_digest(payload, event)
                if operation_id in completion_intents:
                    raise _error(
                        "idempotency_conflict",
                        "completion decision operation_id was already recorded",
                        event_hash=event.entry_hash,
                    )
                completion_intents[operation_id] = PendingIntent(
                    kind="completion",
                    operation="complete",
                    operation_id=operation_id,
                    idempotency_key=key,
                    request_sha256=digest,
                    descriptor=_freeze(
                        {
                            "decision": payload["decision"],
                            "gate_event_hash": event.entry_hash,
                        }
                    ),
                    event_hash=event.entry_hash,
                )
                continue

            if _is_correlated_approval_rejection(event, task_id):
                payload = _common_payload(event, task_id)
                operation_id = _required_text(payload, "operation_id", event)
                key = _required_text(payload, "idempotency_key", event)
                digest = _request_digest(payload, event)
                stage = _required_text(payload, "stage", event)
                if stage not in {"plan", "final"}:
                    raise _error(
                        "invalid_task_event",
                        "approval rejection has an invalid stage",
                        event_hash=event.entry_hash,
                    )
                _required_text(payload, "approver", event)
                _required_text(payload, "rationale", event)
                scope = ("approve", key)
                if scope in idempotency or operation_id in operation_ids:
                    raise _error(
                        "idempotency_conflict",
                        "approval rejection key or operation_id was already recorded",
                        event_hash=event.entry_hash,
                    )
                idempotency[scope] = IdempotencyRecord(
                    operation="approve",
                    idempotency_key=key,
                    request_sha256=digest,
                    operation_id=operation_id,
                    response=_freeze(
                        {
                            "stage": stage,
                            "approved": False,
                            "decision_event_hash": event.entry_hash,
                        }
                    ),
                    outcome="completed",
                    event_hashes=(event.entry_hash,),
                )
                operation_ids[operation_id] = scope
                authoritative_rejections[operation_id] = event
                terminal_hash = event.entry_hash
                continue

            if event.event_type == TASK_OPERATION:
                payload = _common_payload(event, task_id)
                operation = _required_text(payload, "operation", event)
                operation_id = _required_text(payload, "operation_id", event)
                key = _required_text(payload, "idempotency_key", event)
                digest = _request_digest(payload, event)
                phase_blocked = any(
                    phase["status"] == "blocked" for phase in workflow_phases
                )
                phase_approval = bool(
                    operation == "approve"
                    and isinstance(payload.get("request"), Mapping)
                    and payload["request"].get("stage") == "phase"
                )
                if phase_blocked and not phase_approval:
                    raise _error(
                        "invalid_task_state",
                        "blocked workflow phase only accepts phase approval",
                        event_hash=event.entry_hash,
                    )
                scope = (operation, key)
                if scope in idempotency or operation_id in operation_ids:
                    rejection = authoritative_rejections.get(operation_id)
                    existing = idempotency.get(scope)
                    response = _response(payload, event)
                    if (
                        rejection is not None
                        and existing is not None
                        and operation == "approve"
                        and existing.operation_id == operation_id
                        and existing.request_sha256 == digest
                        and isinstance(response, Mapping)
                        and response.get("approved") is False
                        and response.get("decision_event_hash")
                        == rejection.entry_hash
                    ):
                        idempotency[scope] = replace(
                            existing,
                            response=response,
                            outcome=_outcome(payload, event),
                            event_hashes=existing.event_hashes + (event.entry_hash,),
                        )
                        continue
                    if state in {TaskState.VERIFIED, TaskState.REJECTED}:
                        raise _error(
                            "invalid_terminal_task",
                            "terminal task contains a new task operation",
                            event_hash=event.entry_hash,
                        )
                    raise _error(
                        "idempotency_conflict",
                        "task operation key or operation_id was already recorded",
                        event_hash=event.entry_hash,
                        operation=operation,
                        idempotency_key=key,
                        operation_id=operation_id,
                    )
                if state in {TaskState.VERIFIED, TaskState.REJECTED}:
                    raise _error(
                        "invalid_terminal_task",
                        "terminal task contains a new task operation",
                        event_hash=event.entry_hash,
                    )
                response = _response(payload, event)
                record = IdempotencyRecord(
                    operation=operation,
                    idempotency_key=key,
                    request_sha256=digest,
                    operation_id=operation_id,
                    request=_freeze(payload.get("request")),
                    response=response,
                    outcome=_outcome(payload, event),
                    event_hashes=(event.entry_hash,),
                )
                idempotency[scope] = record
                operation_ids[operation_id] = scope

                if operation == "phase":
                    if state is not TaskState.EXECUTING or record.outcome != "completed":
                        raise _error(
                            "invalid_task_event",
                            "phase operation requires executing state and completed outcome",
                            event_hash=event.entry_hash,
                        )
                    phase_value = (
                        response.get("phase")
                        if isinstance(response, Mapping)
                        else None
                    )
                    fields = {
                        "phase_id",
                        "from_status",
                        "status",
                        "attempt",
                        "evidence_hashes",
                    }
                    if (
                        not isinstance(response, Mapping)
                        or set(response) != {"phase"}
                        or not isinstance(phase_value, Mapping)
                        or set(phase_value) != fields
                    ):
                        raise _error(
                            "invalid_task_event",
                            "phase operation response is invalid",
                            event_hash=event.entry_hash,
                        )
                    expected = _current_workflow_phase(workflow_phases)
                    phase_id = phase_value.get("phase_id")
                    from_status = phase_value.get("from_status")
                    status_value = phase_value.get("status")
                    attempt = phase_value.get("attempt")
                    evidence_hashes = phase_value.get("evidence_hashes")
                    phase_request = payload.get("request")
                    if (
                        expected is None
                        or phase_id != expected["phase_id"]
                        or from_status != expected["status"]
                        or isinstance(attempt, bool)
                        or not isinstance(attempt, int)
                        or not isinstance(evidence_hashes, (list, tuple))
                        or any(
                            not isinstance(item, str)
                            or item == event.entry_hash
                            or item not in seen_events
                            or (
                                status_value != "running"
                                and seen_positions[item] <= expected["start_position"]
                            )
                            for item in evidence_hashes
                        )
                    ):
                        raise _error(
                            "invalid_task_event",
                            "phase operation does not match current workflow state",
                            event_hash=event.entry_hash,
                        )
                    if status_value == "running":
                        expected_request = {
                            "action": "start",
                            "phase_id": phase_id,
                        }
                        valid = (
                            from_status in {"pending", "failed"}
                            and attempt == expected["attempt"] + 1
                            and not evidence_hashes
                            and not any(
                                item["status"] == "running"
                                for item in workflow_phases
                                if item is not expected
                            )
                        )
                    else:
                        expected_request = {
                            "action": "finish",
                            "phase_id": phase_id,
                            "status": status_value,
                            "evidence_refs": list(evidence_hashes),
                        }
                        valid = (
                            from_status == "running"
                            and status_value in {"passed", "failed", "blocked"}
                            and attempt == expected["attempt"]
                            and bool(evidence_hashes)
                        )
                    valid = bool(
                        valid
                        and isinstance(phase_request, Mapping)
                        and _thaw(phase_request) == expected_request
                        and canonical_sha256(expected_request) == digest
                    )
                    if not valid:
                        raise _error(
                            "invalid_task_event",
                            "workflow phase transition is invalid",
                            event_hash=event.entry_hash,
                        )
                    if status_value != "running":
                        phase = WorkflowPhase(
                            phase_id=expected["phase_id"],
                            playbook=expected["playbook"],
                            name=expected["name"],
                            steps=tuple(expected["steps"]),
                            requires_action=expected["requires_action"],
                            requires_verification=expected["requires_verification"],
                            requires_verdicts=expected["requires_verdicts"],
                            status=expected["status"],
                            attempt=expected["attempt"],
                            evidence_hashes=tuple(expected["evidence_hashes"]),
                        )
                        _validate_phase_evidence_events(
                            scoped[: seen_positions[event.entry_hash]],
                            phase,
                            tuple(evidence_hashes),
                            start_index=expected["start_position"],
                            status=status_value,
                        )
                    if status_value == "blocked":
                        phase_block_hash = event.entry_hash
                    expected.update(
                        {
                            "status": status_value,
                            "attempt": attempt,
                            "evidence_hashes": tuple(evidence_hashes),
                            "start_position": (
                                seen_positions[event.entry_hash]
                                if status_value == "running"
                                else expected["start_position"]
                            ),
                        }
                    )
                elif operation == "hypothesis":
                    request_value = payload.get("request")
                    response_value = response if isinstance(response, Mapping) else None
                    request_fields = {
                        "phase_id",
                        "hypothesis",
                        "verifier",
                        "status",
                        "rationale",
                        "evidence_refs",
                    }
                    response_fields = {
                        "phase_id",
                        "status",
                        "evidence_refs",
                        "hypothesis_count",
                        "failed_hypotheses",
                    }
                    current = _current_workflow_phase(workflow_phases)
                    if (
                        state is not TaskState.EXECUTING
                        or record.outcome != "completed"
                        or not isinstance(request_value, Mapping)
                        or set(request_value) != request_fields
                        or not isinstance(response_value, Mapping)
                        or set(response_value) != response_fields
                        or current is None
                        or current["status"] != "running"
                    ):
                        raise _error(
                            "invalid_task_event",
                            "hypothesis operation shape or state is invalid",
                            event_hash=event.entry_hash,
                        )
                    phase_id = request_value.get("phase_id")
                    hypothesis = request_value.get("hypothesis")
                    verifier = request_value.get("verifier")
                    status_value = request_value.get("status")
                    rationale = request_value.get("rationale")
                    evidence_refs = request_value.get("evidence_refs")
                    if (
                        phase_id != current["phase_id"]
                        or (current["playbook"], current["name"])
                        not in {
                            ("root-cause-protocol", "hypothesis"),
                            ("debugging", "isolate"),
                        }
                        or any(
                            not isinstance(value, str) or not value.strip()
                            for value in (phase_id, hypothesis, verifier, rationale)
                        )
                        or status_value
                        not in {"supported", "rejected", "inconclusive"}
                        or not isinstance(evidence_refs, (list, tuple))
                        or canonical_sha256(_thaw(request_value)) != digest
                    ):
                        raise _error(
                            "invalid_task_event",
                            "hypothesis operation request is invalid",
                            event_hash=event.entry_hash,
                        )
                    phase = WorkflowPhase(
                        phase_id=current["phase_id"],
                        playbook=current["playbook"],
                        name=current["name"],
                        steps=tuple(current["steps"]),
                        requires_action=current["requires_action"],
                        requires_verification=current["requires_verification"],
                        requires_verdicts=current["requires_verdicts"],
                        status=current["status"],
                        attempt=current["attempt"],
                        evidence_hashes=tuple(current["evidence_hashes"]),
                    )
                    evidence = _validate_phase_evidence_events(
                        scoped[: seen_positions[event.entry_hash]],
                        phase,
                        tuple(evidence_refs),
                        start_index=current["start_position"],
                        status="failed",
                    )
                    hypothesis_count += int(status_value == "rejected")
                    failed_hypotheses += int(status_value == "rejected")
                    if status_value == "rejected":
                        rejected_hypothesis_hashes.append(event.entry_hash)
                    expected_response = {
                        "phase_id": phase_id,
                        "status": status_value,
                        "evidence_refs": list(evidence),
                        "hypothesis_count": hypothesis_count,
                        "failed_hypotheses": failed_hypotheses,
                    }
                    if _thaw(response_value) != expected_response:
                        raise _error(
                            "invalid_task_event",
                            "hypothesis operation response is invalid",
                            event_hash=event.entry_hash,
                        )
                    if (
                        max_failed_hypotheses
                        and failed_hypotheses >= max_failed_hypotheses
                    ):
                        current.update(
                            {
                                "status": "blocked",
                                "evidence_hashes": tuple(evidence),
                            }
                        )
                elif operation == "approve":
                    request_value = payload.get("request")
                    if (
                        isinstance(request_value, Mapping)
                        and request_value.get("stage") == "phase"
                    ):
                        response_value = (
                            response if isinstance(response, Mapping) else None
                        )
                        current = _current_workflow_phase(workflow_phases)
                        phase_id = request_value.get("phase_id")
                        approved = request_value.get("approved")
                        refs = request_value.get("evidence_refs")
                        decision_hash = (
                            response_value.get("decision_event_hash")
                            if isinstance(response_value, Mapping)
                            else None
                        )
                        decision_event = next(
                            (
                                prior
                                for prior in scoped[
                                    : seen_positions[event.entry_hash]
                                ]
                                if prior.entry_hash == decision_hash
                            ),
                            None,
                        )
                        expected_request_fields = {
                            "stage",
                            "approved",
                            "approver",
                            "rationale",
                            "evidence_refs",
                            "phase_id",
                        }
                        expected_response = {
                            "stage": "phase",
                            "approved": approved,
                            "phase_id": phase_id,
                            "decision_event_hash": decision_hash,
                        }
                        required_phase_refs = (
                            set(rejected_hypothesis_hashes)
                            | {hypothesis_gate_hash}
                            if hypothesis_gate_hash is not None
                            else {phase_block_hash}
                            if phase_block_hash is not None
                            else set()
                        )
                        if (
                            state is not TaskState.BLOCKED
                            or record.outcome != "completed"
                            or set(request_value) != expected_request_fields
                            or not isinstance(approved, bool)
                            or not isinstance(refs, (list, tuple))
                            or not refs
                            or any(
                                not isinstance(ref, str)
                                or not _SHA256.fullmatch(ref)
                                or ref not in seen_events
                                for ref in refs
                            )
                            or len(refs) != len(set(refs))
                            or set(refs) != required_phase_refs
                            or current is None
                            or current["status"] != "blocked"
                            or phase_id != current["phase_id"]
                            or canonical_sha256(_thaw(request_value)) != digest
                            or _thaw(response_value) != expected_response
                            or decision_event is None
                            or decision_event.event_type
                            != AuditEventType.HUMAN_DECISION.value
                            or decision_event.payload.get("operation") != "approve"
                            or decision_event.payload.get("idempotency_key") != key
                            or decision_event.payload.get("request_sha256") != digest
                            or decision_event.payload.get("stage") != "phase"
                            or decision_event.payload.get("approved") is not approved
                            or decision_event.payload.get("approver")
                            != request_value.get("approver")
                            or decision_event.payload.get("rationale")
                            != request_value.get("rationale")
                            or (
                                approved
                                and decision_event.payload.get("evidence_refs")
                                != list(refs)
                            )
                        ):
                            raise _error(
                                "invalid_task_event",
                                "phase approval operation is invalid",
                                event_hash=event.entry_hash,
                            )
                        if approved:
                            current["status"] = "failed"
                            failed_hypotheses = 0
                            rejected_hypothesis_hashes.clear()
                            hypothesis_gate_hash = None
                            phase_block_hash = None
                elif operation == "complete":
                    completion_intent = completion_intents.get(operation_id)
                    response_map = response if isinstance(response, Mapping) else {}
                    if completion_intent is not None:
                        if (
                            completion_intent.idempotency_key != key
                            or completion_intent.request_sha256 != digest
                            or response_map.get("gate_event_hash")
                            != completion_intent.event_hash
                        ):
                            raise _error(
                                "invalid_task_event",
                                "complete operation differs from its escalation decision",
                                event_hash=event.entry_hash,
                        )
                        completion_intents.pop(operation_id)
                    gate_response = response_map.get("gate")
                    if (
                        isinstance(gate_response, Mapping)
                        and gate_response.get("decision")
                        in {GateDecision.ESCALATE.value, GateDecision.STOP.value}
                    ):
                        completion_blocks[operation_id] = PendingIntent(
                            kind="completion",
                            operation="complete",
                            operation_id=operation_id,
                            idempotency_key=key,
                            request_sha256=digest,
                            descriptor=_freeze(
                                {
                                    "decision": gate_response["decision"],
                                    "gate_event_hash": response_map.get(
                                        "gate_event_hash"
                                    ),
                                }
                            ),
                            event_hash=event.entry_hash,
                        )
                elif operation == "verify":
                    response_map = response if isinstance(response, Mapping) else {}
                    requirement_id = payload.get("requirement_id") or response_map.get(
                        "requirement_id"
                    )
                    if not isinstance(requirement_id, str) or not requirement_id.strip():
                        raise _error(
                            "invalid_task_event",
                            "verify operation must identify requirement_id",
                            event_hash=event.entry_hash,
                        )
                    requirement_results[requirement_id.strip()] = _freeze(
                        {
                            "outcome": record.outcome,
                            "response": _thaw(response),
                            "event_hash": event.entry_hash,
                        }
                    )
                elif operation == "resolve":
                    response_map = response if isinstance(response, Mapping) else {}
                    target_id = response_map.get("operation_id") or response_map.get(
                        "target_operation_id"
                    )
                    resolution = response_map.get("resolution")
                    if target_id not in pending or resolution not in {
                        "applied",
                        "not_applied",
                        "reject",
                    }:
                        raise _error(
                            "invalid_recovery",
                            "resolve must target one pending action with a valid resolution",
                            event_hash=event.entry_hash,
                            operation_id=target_id,
                            resolution=resolution,
                        )
                    intent = pending.pop(target_id)
                    original_scope = (intent.operation, intent.idempotency_key)
                    original = idempotency[original_scope]
                    idempotency[original_scope] = replace(
                        original,
                        response=_freeze(
                            {
                                "resolution": resolution,
                                "resolution_event_hash": event.entry_hash,
                            }
                        ),
                        outcome=f"resolved:{resolution}",
                        event_hashes=original.event_hashes + (event.entry_hash,),
                    )
                continue

            if event.event_type == TASK_ACTION_INTENT:
                payload = _common_payload(event, task_id)
                if state is not TaskState.EXECUTING:
                    raise _error(
                        "invalid_task_state",
                        "action intent requires executing state",
                        event_hash=event.entry_hash,
                        state=state.value,
                    )
                operation = _required_text(payload, "operation", event)
                operation_id = _required_text(payload, "operation_id", event)
                key = _required_text(payload, "idempotency_key", event)
                digest = _request_digest(payload, event)
                if "descriptor" not in payload:
                    raise _error(
                        "invalid_task_event",
                        "task_action_intent.descriptor is required",
                        event_hash=event.entry_hash,
                    )
                scope = (operation, key)
                if scope in idempotency or operation_id in operation_ids:
                    raise _error(
                        "idempotency_conflict",
                        "action key or operation_id was already recorded",
                        event_hash=event.entry_hash,
                    )
                descriptor = _freeze(payload["descriptor"])
                intent = PendingIntent(
                    kind="action",
                    operation=operation,
                    operation_id=operation_id,
                    idempotency_key=key,
                    request_sha256=digest,
                    descriptor=descriptor,
                    event_hash=event.entry_hash,
                )
                pending[operation_id] = intent
                idempotency[scope] = IdempotencyRecord(
                    operation=operation,
                    idempotency_key=key,
                    request_sha256=digest,
                    operation_id=operation_id,
                    request=descriptor,
                    event_hashes=(event.entry_hash,),
                )
                operation_ids[operation_id] = scope
                continue

            if event.event_type == TASK_ACTION_RESULT:
                payload = _common_payload(event, task_id)
                operation = _required_text(payload, "operation", event)
                operation_id = _required_text(payload, "operation_id", event)
                key = _required_text(payload, "idempotency_key", event)
                digest = _request_digest(payload, event)
                intent = pending.get(operation_id)
                if intent is None:
                    raise _error(
                        "orphan_action_result",
                        "action result has no pending intent",
                        event_hash=event.entry_hash,
                        operation_id=operation_id,
                    )
                if (
                    intent.operation != operation
                    or intent.idempotency_key != key
                    or intent.request_sha256 != digest
                ):
                    raise _error(
                        "action_result_mismatch",
                        "action result identity differs from its intent",
                        event_hash=event.entry_hash,
                        operation_id=operation_id,
                    )
                if (
                    "descriptor" not in payload
                    or _freeze(payload["descriptor"]) != intent.descriptor
                ):
                    raise _error(
                        "action_result_mismatch",
                        "action result descriptor differs from its intent",
                        event_hash=event.entry_hash,
                        operation_id=operation_id,
                    )
                provenance = payload.get("provenance_event_hashes")
                if not isinstance(provenance, list) or not provenance or any(
                    not isinstance(item, str)
                    or item == event.entry_hash
                    or item not in seen_events
                    for item in provenance
                ):
                    raise _error(
                        "invalid_action_provenance",
                        "action result must cite earlier ledger evidence",
                        event_hash=event.entry_hash,
                        operation_id=operation_id,
                    )
                if "result" not in payload:
                    raise _error(
                        "invalid_task_event",
                        "task_action_result.result is required",
                        event_hash=event.entry_hash,
                    )
                if operation == "verify" and not isinstance(
                    payload["result"], Mapping
                ):
                    raise _error(
                        "invalid_task_event",
                        "verification action result must be an object",
                        event_hash=event.entry_hash,
                        operation_id=operation_id,
                    )
                scope = (operation, key)
                original = idempotency[scope]
                idempotency[scope] = replace(
                    original,
                    response=_response(payload, event),
                    outcome=_outcome(payload, event),
                    event_hashes=original.event_hashes + (event.entry_hash,),
                )
                pending.pop(operation_id)
                if (
                    workflow_phases
                    and _verification_result_requires_phase_block(
                        event,
                        seen_events,
                    )
                ):
                    current = _current_workflow_phase(workflow_phases)
                    if current is None or current["status"] != "running":
                        raise _error(
                            "invalid_task_event",
                            "blocking verification requires a running workflow phase",
                            event_hash=event.entry_hash,
                        )
                    current.update(
                        {
                            "status": "blocked",
                            "evidence_hashes": tuple(
                                dict.fromkeys((*provenance, event.entry_hash))
                            ),
                        }
                    )
                    phase_block_hash = event.entry_hash
                continue

            if event.event_type == TASK_REFLECTION_INTENT:
                payload = _common_payload(event, task_id)
                reflection_id = _required_text(payload, "reflection_id", event)
                source_hash = _required_text(payload, "source_event_hash", event)
                if terminal_hash is None or source_hash != terminal_hash:
                    raise _error(
                        "invalid_reflection",
                        "reflection must cite the current terminal transition",
                        event_hash=event.entry_hash,
                        source_event_hash=source_hash,
                        terminal_event_hash=terminal_hash,
                    )
                if reflection is not None or reflection_intent is not None:
                    raise _error(
                        "reflection_conflict",
                        "task already has a reflection intent or manifest",
                        event_hash=event.entry_hash,
                    )
                key = payload.get("idempotency_key", reflection_id)
                digest = payload.get(
                    "request_sha256", canonical_sha256({"source_event_hash": source_hash})
                )
                if not isinstance(key, str) or not key.strip() or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                    raise _error(
                        "invalid_task_event",
                        "reflection idempotency fields are invalid",
                        event_hash=event.entry_hash,
                    )
                reflection_intent = PendingIntent(
                    kind="reflection",
                    operation="reflect",
                    operation_id=reflection_id,
                    idempotency_key=key.strip(),
                    request_sha256=digest,
                    descriptor=_freeze({"source_event_hash": source_hash}),
                    event_hash=event.entry_hash,
                )
                continue

            if event.event_type == TASK_REFLECTED:
                payload = _common_payload(event, task_id)
                reflection_id = _required_text(payload, "reflection_id", event)
                source_hash = _required_text(payload, "source_event_hash", event)
                if (
                    reflection_intent is None
                    or reflection_intent.operation_id != reflection_id
                    or reflection_intent.descriptor["source_event_hash"] != source_hash
                ):
                    raise _error(
                        "orphan_reflection_result",
                        "reflection manifest has no matching intent",
                        event_hash=event.entry_hash,
                    )
                memory_ids = payload.get("memory_entry_ids")
                if not isinstance(memory_ids, list) or any(
                    not isinstance(item, str) or not item.strip() for item in memory_ids
                ) or len(memory_ids) != len(set(memory_ids)):
                    raise _error(
                        "invalid_reflection",
                        "memory_entry_ids must be a unique string array",
                        event_hash=event.entry_hash,
                    )
                reflection = _freeze(
                    {
                        "reflection_id": reflection_id,
                        "source_event_hash": source_hash,
                        "memory_entry_ids": list(memory_ids),
                        "response": _thaw(_response(payload, event)),
                        "event_hash": event.entry_hash,
                    }
                )
                reflection_intent = None
                continue

            # Evidence written by Spec 002 is also useful when a process died
            # before its task_operation result was recorded.
            if event.event_type == "evidence" and event.payload.get("kind") == "verification_result":
                requirement_id = event.payload.get("requirement_id")
                if isinstance(requirement_id, str) and requirement_id.strip():
                    requirement_results[requirement_id.strip()] = _freeze(
                        {
                            "status": event.payload.get("status", "recorded"),
                            "evidence_event_hash": event.entry_hash,
                        }
                    )

        unresolved = tuple(pending.values())
        unresolved += tuple(completion_intents.values())
        unresolved += tuple(completion_blocks.values())
        if reflection_intent is not None:
            unresolved += (reflection_intent,)
        action_orphan = any(intent.kind == "action" for intent in unresolved)
        completion_orphan = any(
            intent.kind == "completion" for intent in unresolved
        )
        rejection_event = (
            tuple(authoritative_rejections.values())[-1]
            if authoritative_rejections
            else None
        )
        if action_orphan and (
            state in {TaskState.VERIFIED, TaskState.REJECTED}
            or rejection_event is not None
        ):
            raise _error(
                "invalid_terminal_task",
                "terminal task contains an unresolved action intent",
                task_id=task_id,
            )
        if completion_orphan and state in {
            TaskState.VERIFIED,
            TaskState.REJECTED,
        }:
            raise _error(
                "invalid_terminal_task",
                "terminal task contains an unfinished completion decision",
                task_id=task_id,
            )
        workflow_blocked = any(
            phase["status"] == "blocked" for phase in workflow_phases
        )
        if rejection_event is not None:
            projected_state = TaskState.REJECTED
            terminal_hash = terminal_hash or rejection_event.entry_hash
        elif action_orphan or completion_orphan or workflow_blocked:
            projected_state = TaskState.BLOCKED
        else:
            projected_state = state

        frozen_idempotency = MappingProxyType(dict(idempotency))
        frozen_requirements = MappingProxyType(dict(requirement_results))
        frozen_contract = _freeze(dict(contract))
        projected_workflow_phases = tuple(
            WorkflowPhase(
                phase_id=phase["phase_id"],
                playbook=phase["playbook"],
                name=phase["name"],
                steps=tuple(phase["steps"]),
                requires_action=phase["requires_action"],
                requires_verification=phase["requires_verification"],
                requires_verdicts=phase["requires_verdicts"],
                status=phase["status"],
                attempt=phase["attempt"],
                evidence_hashes=tuple(phase["evidence_hashes"]),
            )
            for phase in workflow_phases
        )
        current_workflow_phase = _current_workflow_phase(workflow_phases)
        projected_current_phase = next(
            (
                phase
                for phase in projected_workflow_phases
                if current_workflow_phase is not None
                and phase.phase_id == current_workflow_phase["phase_id"]
            ),
            None,
        )
        session = TaskSession(
            schema_version=SCHEMA_VERSION,
            task_id=task_id,
            contract_id=task_id,
            contract_snapshot=frozen_contract,
            contract_hash=canonical_sha256(contract),
            state=projected_state,
            phase=_phase(projected_state, unresolved, reflection),
            requirement_results=frozen_requirements,
            idempotency=frozen_idempotency,
            unresolved_intents=unresolved,
            reflection=reflection,
            event_hashes=tuple(event.entry_hash for event in scoped),
            terminal=projected_state in {TaskState.VERIFIED, TaskState.REJECTED},
            allowed_next=_workflow_allowed_next(
                projected_state,
                unresolved,
                reflection,
                projected_workflow_phases,
                projected_current_phase,
            ),
            hypothesis_count=hypothesis_count,
            blocked_reason=(
                "unresolved action intent"
                if action_orphan
                else "unfinished completion decision"
                if completion_orphan
                else "workflow recovery requires an exact operation replay"
                if workflow_blocked
                and projected_state is TaskState.BLOCKED
                and state is not TaskState.BLOCKED
                else "workflow phase requires human review"
                if workflow_blocked and projected_state is TaskState.BLOCKED
                else None
            ),
            workflow=workflow,
            workflow_phases=projected_workflow_phases,
            current_phase_id=(
                current_workflow_phase["phase_id"]
                if current_workflow_phase is not None
                else None
            ),
        )
        if workflow_blocked and projected_state is TaskState.BLOCKED:
            session = replace(
                session,
                approval_evidence_refs=self._phase_approval_evidence(
                    session,
                    allow_pending=state is not TaskState.BLOCKED,
                ),
            )
            if state is not TaskState.BLOCKED:
                if self._hypothesis_block_record(session) is not None:
                    recovery = "hypothesis"
                else:
                    cause = self._phase_block_event(session)
                    recovery = (
                        "verify"
                        if cause is not None
                        and cause.event_type == TASK_ACTION_RESULT
                        and cause.payload.get("operation") == "verify"
                        else "phase_finish"
                    )
                session = replace(session, allowed_next=(recovery, "reject"))
        return session


__all__ = [
    "IdempotencyRecord",
    "PendingIntent",
    "TaskLifecycle",
    "TaskLifecycleError",
    "TaskSession",
    "TaskState",
    "WorkflowPhase",
    "canonical_sha256",
]
