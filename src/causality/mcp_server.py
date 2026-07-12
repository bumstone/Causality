from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from .agent_bootstrap import (
    SUPPORTED_CLIENTS,
    _assert_safe_install_path,
    _ensure_private_ignore,
    _private_tracking_issue,
    install_agent_files,
)
from .browser_adapter import A11yBrowserAdapter, wrap_untrusted
from .contracts import (
    EvidenceKind,
    EvidenceRequirement,
    GoalContract,
    IRREVERSIBLE_ACTIONS,
    PermissionContract,
    VerificationRequirement,
)
from .http_adapter import HttpAdapter
from .ledger import EvidenceLedger
from .memory import TypedMemory
from .skills import SkillPromotionError, SkillStore
from .task_lifecycle import (
    TaskLifecycle,
    TaskLifecycleError,
    TaskPolicy,
    TaskSession,
)
from .workflows import workflow_manifest


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    return value


def _text_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    result = {
        "content": [
            {
                "type": "text",
                "text": value if isinstance(value, str) else json.dumps(
                    _plain(value), ensure_ascii=True, sort_keys=True
                ),
            }
        ]
    }
    if is_error:
        result["isError"] = True
    return result


def _closed(
    properties: Mapping[str, Any],
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": False,
    }
    if required:
        value["required"] = list(required)
    return value


_TEXT = {"type": "string", "minLength": 1}
_KEY = {
    "type": "string",
    "minLength": 1,
    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
}
_HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_REF = {"type": "string", "pattern": "^@[ec][0-9]+$"}
_COMMON = {"task_id": _TEXT, "idempotency_key": _KEY}
_BROWSER_TOOLS = frozenset(
    {
        "browser.observe",
        "browser.act",
        "browser.assert",
        "browser.inspect",
        "browser.visual",
    }
)
_MCP_EVIDENCE_KINDS = tuple(
    kind.value
    for kind in (
        EvidenceKind.TEST_OUTPUT,
        EvidenceKind.BROWSER_DIFF,
        EvidenceKind.ARTIFACT_HASH,
        EvidenceKind.TOOL_OUTPUT,
        EvidenceKind.A11Y_REPORT,
        EvidenceKind.VERIFICATION_RESULT,
    )
)


def _tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": schema}


def _command_policy_from_env(*, browser_enabled: bool = False) -> TaskPolicy:
    def commands(name: str) -> tuple[tuple[str, ...], ...]:
        raw = os.environ.get(name)
        if not raw:
            return ()
        value = json.loads(raw)
        if not isinstance(value, list) or any(
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) or not item for item in argv)
            for argv in value
        ):
            raise ValueError(f"{name} must be a JSON array of non-empty argv arrays")
        return tuple(tuple(argv) for argv in value)

    def strings(name: str) -> frozenset[str]:
        raw = os.environ.get(name)
        if not raw:
            return frozenset()
        value = json.loads(raw)
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ValueError(f"{name} must be a JSON array of non-empty strings")
        return frozenset(value)

    origins = strings("CAUSALITY_NETWORK_ORIGINS_JSON")
    auth_refs = strings("CAUSALITY_AUTH_REFS_JSON")
    allowed_tools = frozenset({"shell", "file.read", "file.write"})
    if origins:
        allowed_tools |= {"http"}
    if browser_enabled:
        allowed_tools |= _BROWSER_TOOLS
    return TaskPolicy(
        allowed_tools=allowed_tools,
        allowed_network_origins=origins,
        allowed_auth_refs=auth_refs,
        allowed_http_headers=strings("CAUSALITY_HTTP_HEADERS_JSON"),
        subprocess_argv_prefixes=commands("CAUSALITY_SUBPROCESS_PREFIXES_JSON"),
        verification_commands=commands("CAUSALITY_VERIFICATION_COMMANDS_JSON"),
        verification_argv_prefixes=(
            (sys.executable, "-m", "unittest"),
            (sys.executable, "-m", "pytest"),
            *commands("CAUSALITY_VERIFICATION_PREFIXES_JSON"),
        ),
    )


def _browser_command_from_env() -> tuple[str, ...] | None:
    name = "CAUSALITY_BROWSER_COMMAND_JSON"
    raw = os.environ.get(name)
    if raw:
        value = json.loads(raw)
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item for item in value)
        ):
            raise ValueError(f"{name} must be a non-empty argv array")
        return tuple(value)
    legacy = os.environ.get("CAUSALITY_BROWSER_BIN")
    if legacy:
        return (legacy,)
    return None


def _http_credentials_from_env() -> dict[str, dict[str, str]]:
    name = "CAUSALITY_HTTP_CREDENTIALS_JSON"
    raw = os.environ.get(name)
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    credentials: dict[str, dict[str, str]] = {}
    for alias, headers in value.items():
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or not isinstance(headers, dict)
            or not headers
            or any(
                not isinstance(header, str) or not isinstance(secret, str)
                for header, secret in headers.items()
            )
        ):
            raise ValueError(
                f"{name} must map non-empty aliases to non-empty string header objects"
            )
        credentials[alias] = dict(headers)
    return credentials


class CausalityMCPServer:
    """Stdio JSON-RPC adapter for the durable task lifecycle."""

    def __init__(
        self,
        project: str | Path = ".",
        *,
        approval_token: str | None = None,
        policy: TaskPolicy | None = None,
        http_credentials: Mapping[str, Mapping[str, str]] | None = None,
        http_adapter: HttpAdapter | None = None,
        browser_adapter: A11yBrowserAdapter | None = None,
    ):
        self.project = Path(project).resolve()
        tracking_issue = _private_tracking_issue(self.project)
        if tracking_issue:
            raise ValueError(tracking_issue)
        causality_dir = self.project / ".causality"
        privacy_path = causality_dir / ".gitignore"
        ledger_path = causality_dir / "ledger.jsonl"
        _assert_safe_install_path(self.project, privacy_path)
        _assert_safe_install_path(self.project, ledger_path)
        _assert_safe_install_path(self.project, Path(str(ledger_path) + ".lock"))
        causality_dir.mkdir(parents=True, exist_ok=True)
        _ensure_private_ignore(privacy_path)
        self.ledger = EvidenceLedger(ledger_path)
        self._approval_token = approval_token or os.environ.get(
            "CAUSALITY_APPROVAL_TOKEN"
        )
        browser_command = (
            None if browser_adapter is not None else _browser_command_from_env()
        )
        effective_browser = browser_adapter or (
            A11yBrowserAdapter(browser_command) if browser_command is not None else None
        )
        effective_policy = policy or _command_policy_from_env(
            browser_enabled=effective_browser is not None
        )
        credentials = (
            _http_credentials_from_env()
            if http_credentials is None
            else dict(http_credentials)
        )
        unknown_credentials = set(credentials) - effective_policy.allowed_auth_refs
        if unknown_credentials:
            raise ValueError(
                "HTTP credential aliases must be explicitly allowed by server policy"
            )
        self.lifecycle = TaskLifecycle(
            self.project,
            self.ledger.path,
            policy=effective_policy,
            approval_authorizer=self._authorize,
            http_credentials=credentials,
            http_adapter=http_adapter,
            browser_adapter=effective_browser,
        )
        self.skills = SkillStore(self.project)

    def _authorize(self, _principal: str, _stage: str, proof: str | None) -> bool:
        return bool(
            self._approval_token
            and isinstance(proof, str)
            and hmac.compare_digest(proof, self._approval_token)
        )

    def handle(self, request: Any) -> dict[str, Any] | None:
        if not isinstance(request, dict):
            return self._error(None, -32600, "invalid JSON-RPC request")
        request_id = request.get("id")
        if "id" not in request:
            return None
        method = request.get("method")
        if not isinstance(method, str):
            return self._error(request_id, -32600, "request method must be a string")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "causality", "version": "0.1.0"},
                }
            elif method == "tools/list":
                result = {"tools": self._tools()}
            elif method == "tools/call":
                params = request.get("params")
                if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                    return self._error(request_id, -32602, "invalid tools/call params")
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict):
                    return self._error(request_id, -32602, "tool arguments must be an object")
                result = self._call_tool(params["name"], arguments)
            else:
                return self._error(request_id, -32601, f"unknown method: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:  # pragma: no cover - defensive protocol boundary
            return self._error(request_id, -32000, f"{type(exc).__name__}: {exc}")

    def _tools(self) -> list[dict[str, Any]]:
        permissions = _closed(
            {
                "allowed_tools": {"type": "array", "items": {"type": "string"}},
                "write_scope": {"type": "array", "items": {"type": "string"}},
                "network_scope": {"type": "array", "items": {"type": "string"}},
                "auth_scope": {"type": "array", "items": {"type": "string"}},
            },
            ("allowed_tools", "write_scope", "network_scope", "auth_scope"),
        )
        verification = _closed(
            {
                "id": _TEXT,
                "argv": {"type": "array", "items": {"type": "string"}},
                "expected_exit_codes": {"type": "array", "items": {"type": "integer"}},
                "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
                "artifact_paths": {"type": "object"},
                "required": {"type": "boolean"},
                "manual": {"type": "boolean"},
            },
            ("id", "argv", "required", "manual"),
        )
        stop = _closed(
            {
                "max_iterations": {"type": "integer", "minimum": 1},
                "max_failed_hypotheses": {"type": "integer", "minimum": 1},
                "no_progress_iterations": {"type": "integer", "minimum": 1},
            },
            ("max_iterations", "max_failed_hypotheses", "no_progress_iterations"),
        )
        actions = {
            "oneOf": [
                _closed({"kind": {"const": "file_read"}, "path": _TEXT}, ("kind", "path")),
                _closed(
                    {"kind": {"const": "file_write"}, "path": _TEXT, "content": {"type": "string"}},
                    ("kind", "path", "content"),
                ),
                _closed(
                    {
                        "kind": {"const": "subprocess"},
                        "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "cwd": _TEXT,
                        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 300},
                    },
                    ("kind", "argv"),
                ),
            ]
        }
        common_required = ("task_id", "idempotency_key")
        browser_branches = [
            _closed(
                {
                    **_COMMON,
                    "operation": {"const": "observe"},
                    "mode": {
                        "type": "string",
                        "enum": ["interactive", "compact", "full"],
                    },
                    "scope": _REF,
                    "annotate": {"type": "boolean"},
                },
                (*common_required, "operation"),
            ),
            _closed(
                {
                    **_COMMON,
                    "operation": {"const": "act"},
                    "action": {
                        "type": "string",
                        "enum": ["click", "fill", "hover", "press", "select"],
                    },
                    "ref": _REF,
                    "value": {"type": "string"},
                    "expected_state_hash": _HASH,
                },
                (*common_required, "operation", "action", "ref", "expected_state_hash"),
            ),
            _closed(
                {
                    **_COMMON,
                    "operation": {"const": "assert"},
                    "property": {
                        "type": "string",
                        "enum": ["visible", "enabled", "checked"],
                    },
                    "ref": _REF,
                    "expected_state_hash": _HASH,
                },
                (*common_required, "operation", "property", "ref", "expected_state_hash"),
            ),
            _closed(
                {
                    **_COMMON,
                    "operation": {"const": "inspect"},
                    "inspection": {
                        "type": "string",
                        "enum": ["attrs", "html", "css"],
                    },
                    "ref": _REF,
                    "expected_state_hash": _HASH,
                },
                (*common_required, "operation", "inspection", "ref", "expected_state_hash"),
            ),
            _closed(
                {
                    **_COMMON,
                    "operation": {"const": "visual"},
                    "ref": _REF,
                    "expected_state_hash": _HASH,
                },
                (*common_required, "operation", "expected_state_hash"),
            ),
        ]
        browser_schema = _closed(
            {
                **_COMMON,
                "operation": {
                    "type": "string",
                    "enum": ["observe", "act", "assert", "inspect", "visual"],
                },
                "mode": {"type": "string"},
                "scope": _REF,
                "annotate": {"type": "boolean"},
                "action": {"type": "string"},
                "ref": _REF,
                "value": {"type": "string"},
                "expected_state_hash": _HASH,
                "property": {"type": "string"},
                "inspection": {"type": "string"},
            }
        )
        browser_schema["oneOf"] = browser_branches
        phase_schema = _closed(
            {
                **_COMMON,
                "phase_id": _TEXT,
                "action": {"type": "string", "enum": ["start", "finish"]},
                "status": {
                    "type": "string",
                    "enum": ["passed", "failed", "blocked"],
                },
                "evidence_refs": {
                    "type": "array",
                    "items": _HASH,
                    "minItems": 1,
                    "uniqueItems": True,
                },
            },
            (*common_required, "phase_id", "action"),
        )
        phase_schema["oneOf"] = [
            {
                "properties": {"action": {"const": "start"}},
                "not": {
                    "anyOf": [
                        {"required": ["status"]},
                        {"required": ["evidence_refs"]},
                    ]
                },
            },
            {
                "properties": {"action": {"const": "finish"}},
                "required": ["status", "evidence_refs"],
            },
        ]
        tools = [
            _tool(
                "causality_init",
                "Install project-level Causality agent files.",
                _closed(
                    {
                        "client": {"type": "string", "enum": list(SUPPORTED_CLIENTS), "default": "auto"},
                        "verify": {"type": "boolean", "default": False},
                    }
                ),
            ),
            _tool(
                "causality_context",
                "Return recent ledger events, active failures, and curated knowledge paths.",
                _closed(
                    {"limit": {"type": "integer", "minimum": 0, "default": 5}}
                ),
            ),
            _tool(
                "causality_task_resume",
                "Read one durable task projection without replaying effects or writing state.",
                _closed({"task_id": _TEXT}, ("task_id",)),
            ),
            _tool(
                "causality_task_begin",
                "Begin one durable task and freeze its contract.",
                _closed(
                    {
                        "objective": _TEXT,
                        "summary": {"type": "string"},
                        "risk": {"type": "string", "enum": ["low", "medium", "high", "irreversible"]},
                        "permissions": permissions,
                        "verification_requirements": {"type": "array", "items": verification, "minItems": 1},
                        "stop_condition": stop,
                        "non_goals": {"type": "array", "items": _TEXT},
                        "evidence_required": {
                            "type": "array",
                            "items": _closed(
                                {
                                    "kind": {
                                        "type": "string",
                                        "enum": list(_MCP_EVIDENCE_KINDS),
                                    },
                                    "description": _TEXT,
                                    "required": {"type": "boolean"},
                                },
                                ("kind", "description", "required"),
                            ),
                        },
                        "workflow": {
                            "type": "string",
                            "enum": ["auto", "root-cause-protocol"],
                            "default": "auto",
                        },
                        "idempotency_key": _KEY,
                    },
                    ("objective", "risk", "permissions", "verification_requirements", "stop_condition", "idempotency_key"),
                ),
            ),
            _tool(
                "causality_task_approve",
                "Record a server-authenticated plan or final decision.",
                _closed(
                    {
                        **_COMMON,
                        "stage": {
                            "type": "string",
                            "enum": ["plan", "final", "phase", *sorted(IRREVERSIBLE_ACTIONS)],
                        },
                        "phase_id": _TEXT,
                        "approved": {"type": "boolean"},
                        "approver": _TEXT,
                        "rationale": _TEXT,
                        "evidence_refs": {"type": "array", "items": _HASH},
                        "proof": _TEXT,
                    },
                    (*common_required, "stage", "approved", "approver", "rationale", "evidence_refs", "proof"),
                ),
            ),
            _tool(
                "causality_task_phase",
                "Start or finish the current durable workflow phase.",
                phase_schema,
            ),
            _tool(
                "causality_task_hypothesis",
                "Record one evidence-backed debugging hypothesis outcome.",
                _closed(
                    {
                        **_COMMON,
                        "phase_id": _TEXT,
                        "hypothesis": _TEXT,
                        "verifier": _TEXT,
                        "status": {
                            "type": "string",
                            "enum": ["supported", "rejected", "inconclusive"],
                        },
                        "rationale": _TEXT,
                        "evidence_refs": {
                            "type": "array",
                            "items": _HASH,
                            "minItems": 1,
                            "uniqueItems": True,
                        },
                    },
                    (
                        *common_required,
                        "phase_id",
                        "hypothesis",
                        "verifier",
                        "status",
                        "rationale",
                        "evidence_refs",
                    ),
                ),
            ),
            _tool(
                "causality_task_action",
                "Execute one typed, gated task action.",
                _closed({**_COMMON, "action": actions}, (*common_required, "action")),
            ),
            _tool(
                "causality_task_http",
                "Execute one scoped, gated HTTP action without persisting secrets.",
                _closed(
                    {
                        **_COMMON,
                        "method": {
                            "type": "string",
                            "enum": [
                                "GET",
                                "HEAD",
                                "POST",
                                "PUT",
                                "PATCH",
                                "DELETE",
                                "OPTIONS",
                            ],
                        },
                        "url": _TEXT,
                        "headers": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                        "body_ref": _TEXT,
                        "timeout_seconds": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "maximum": 300,
                        },
                        "expected_statuses": {
                            "type": "array",
                            "items": {
                                "type": "integer",
                                "minimum": 100,
                                "maximum": 599,
                            },
                            "minItems": 1,
                            "uniqueItems": True,
                        },
                        "response_artifact": _TEXT,
                        "auth_ref": _KEY,
                    },
                    (*common_required, "method", "url", "expected_statuses"),
                ),
            ),
            _tool(
                "causality_task_verify",
                "Execute or manually decide one frozen verification requirement.",
                _closed(
                    {
                        **_COMMON,
                        "requirement_id": _TEXT,
                        "mode": {"type": "string", "enum": ["execute", "manual"]},
                        "evidence_hash": _HASH,
                        "approved": {"type": "boolean"},
                        "approver": _TEXT,
                        "rationale": _TEXT,
                        "proof": _TEXT,
                    },
                    (*common_required, "requirement_id", "mode"),
                ),
            ),
            _tool(
                "causality_task_verdict",
                "Record one task-scoped verifier decision.",
                _closed(
                    {
                        **_COMMON,
                        "verifier": _TEXT,
                        "status": {"type": "string", "enum": ["pass", "fail"]},
                        "rationale": _TEXT,
                        "severity": {"type": "string", "enum": ["normal", "critical"]},
                        "evidence_refs": {"type": "array", "items": _HASH},
                    },
                    (*common_required, "verifier", "status", "rationale", "evidence_refs"),
                ),
            ),
            _tool(
                "causality_task_complete",
                "Evaluate the fixed completion gate.",
                _closed(_COMMON, common_required),
            ),
            _tool(
                "causality_task_resolve",
                "Resolve an uncertain action using authenticated operator evidence.",
                _closed(
                    {
                        **_COMMON,
                        "operation_id": _TEXT,
                        "resolution": {"type": "string", "enum": ["applied", "not_applied", "reject"]},
                        "approver": _TEXT,
                        "rationale": _TEXT,
                        "proof": _TEXT,
                    },
                    (*common_required, "operation_id", "resolution", "approver", "rationale", "proof"),
                ),
            ),
            _tool(
                "causality_task_reflect",
                "Reflect a terminal task exactly once.",
                _closed(
                    {
                        **_COMMON,
                        "scope": _TEXT,
                        "ttl_days": {"type": "integer", "minimum": 1},
                    },
                    common_required,
                ),
            ),
            _tool(
                "causality_append_evidence",
                "DEPRECATED: append typed, declared, task-scoped evidence.",
                _closed(
                    {
                        **_COMMON,
                        "kind": {"type": "string"},
                        "payload": {"type": "object"},
                        "artifact_paths": {"type": "array", "items": _TEXT},
                    },
                    (*common_required, "kind", "payload"),
                ),
            ),
            _tool(
                "causality_workflows",
                "Return the available Causality workflow manifest.",
                _closed({}),
            ),
            _tool(
                "causality_skill_outcome",
                "Record one task-bound reproducibility outcome for an earned skill.",
                _closed({
                    "task_id": _TEXT, "idempotency_key": _KEY, "skill_id": _TEXT,
                    "success": {"type": "boolean"},
                    "evidence_refs": {"type": "array", "items": _HASH},
                }, ("task_id", "idempotency_key", "skill_id", "success", "evidence_refs")),
            ),
            _tool(
                "causality_skill_promote",
                "Promote an earned skill after fixed reproducibility and human approval gates.",
                _closed({
                    "skill_id": _TEXT, "idempotency_key": _KEY, "approved_by": _TEXT,
                    "evidence_refs": {"type": "array", "items": _HASH}, "proof": _TEXT,
                }, ("skill_id", "idempotency_key", "approved_by", "evidence_refs", "proof")),
            ),
        ]
        if self.lifecycle.policy.allowed_tools & _BROWSER_TOOLS:
            tools.insert(
                6,
                _tool(
                    "causality_task_browser",
                    "Execute one state-bound browser operation through a capable isolated driver.",
                    browser_schema,
                ),
            )
        return tools

    @staticmethod
    def _strict(
        arguments: dict[str, Any],
        *,
        allowed: set[str],
        required: set[str] = frozenset(),
    ) -> None:
        unknown = set(arguments) - allowed
        missing = required - set(arguments)
        if unknown or missing:
            raise TaskLifecycleError(
                "validation_error",
                "tool arguments do not match the closed schema",
                details={"unknown": sorted(unknown), "missing": sorted(missing)},
            )

    @staticmethod
    def _string_array(value: Any, name: str) -> tuple[str, ...]:
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise TaskLifecycleError("validation_error", f"{name} must be a string array")
        return tuple(value)

    def _begin(self, arguments: dict[str, Any]) -> TaskSession:
        allowed = {
            "objective", "summary", "risk", "permissions",
            "verification_requirements", "stop_condition", "non_goals",
            "evidence_required", "workflow", "idempotency_key",
        }
        required = {
            "objective", "risk", "permissions", "verification_requirements",
            "stop_condition", "idempotency_key",
        }
        self._strict(arguments, allowed=allowed, required=required)
        if not isinstance(arguments["objective"], str) or not arguments["objective"].strip():
            raise TaskLifecycleError("validation_error", "objective must be non-blank")
        if "summary" in arguments and not isinstance(arguments["summary"], str):
            raise TaskLifecycleError("validation_error", "summary must be a string")
        workflow = arguments.get("workflow")
        if workflow is not None and (
            not isinstance(workflow, str)
            or workflow not in {"auto", "root-cause-protocol"}
        ):
            raise TaskLifecycleError(
                "validation_error",
                "workflow must be auto or root-cause-protocol",
            )
        if workflow is None:
            workflow = "auto"
            for event in self.ledger.events(all_segments=True):
                if (
                    event.event_type == "task_started"
                    and event.payload.get("idempotency_key")
                    == arguments["idempotency_key"]
                    and event.payload.get("workflow", "legacy") == "legacy"
                ):
                    # Preserve retries created before MCP began defaulting to
                    # automatic workflow selection. New omitted requests use auto.
                    workflow = "legacy"
                    break
        permissions = arguments["permissions"]
        if not isinstance(permissions, dict):
            raise TaskLifecycleError("validation_error", "permissions must be an object")
        self._strict(
            permissions,
            allowed={"allowed_tools", "write_scope", "network_scope", "auth_scope"},
            required={"allowed_tools", "write_scope", "network_scope", "auth_scope"},
        )
        permission = PermissionContract(
            allowed_tools=self._string_array(permissions["allowed_tools"], "allowed_tools"),
            write_scope=self._string_array(permissions["write_scope"], "write_scope"),
            network_scope=self._string_array(permissions["network_scope"], "network_scope"),
            auth_scope=self._string_array(permissions["auth_scope"], "auth_scope"),
        )
        raw_requirements = arguments["verification_requirements"]
        if not isinstance(raw_requirements, list) or not raw_requirements:
            raise TaskLifecycleError("validation_error", "verification_requirements must be non-empty")
        requirements: list[VerificationRequirement] = []
        requirement_fields = {
            "id", "argv", "expected_exit_codes", "timeout_seconds",
            "artifact_paths", "required", "manual",
        }
        for raw in raw_requirements:
            if not isinstance(raw, dict):
                raise TaskLifecycleError("validation_error", "verification requirement must be an object")
            self._strict(raw, allowed=requirement_fields, required={"id", "argv", "required", "manual"})
            requirements.append(VerificationRequirement.from_mapping(raw))
        stop = arguments["stop_condition"]
        if not isinstance(stop, dict):
            raise TaskLifecycleError("validation_error", "stop_condition must be an object")
        self._strict(
            stop,
            allowed={"max_iterations", "max_failed_hypotheses", "no_progress_iterations"},
            required={"max_iterations", "max_failed_hypotheses", "no_progress_iterations"},
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in stop.values()):
            raise TaskLifecycleError("validation_error", "stop_condition values must be positive integers")
        non_goals = self._string_array(arguments.get("non_goals", []), "non_goals")
        evidence: list[EvidenceRequirement] = []
        raw_evidence = arguments.get("evidence_required")
        if raw_evidence is None:
            evidence.append(
                EvidenceRequirement(
                    EvidenceKind.TOOL_OUTPUT,
                    "host edit output",
                    required=False,
                )
            )
        else:
            if not isinstance(raw_evidence, list):
                raise TaskLifecycleError(
                    "validation_error", "evidence_required must be an array"
                )
            for raw in raw_evidence:
                if not isinstance(raw, dict):
                    raise TaskLifecycleError(
                        "validation_error", "evidence requirement must be an object"
                    )
                self._strict(
                    raw,
                    allowed={"kind", "description", "required"},
                    required={"kind", "description", "required"},
                )
                kind = raw["kind"]
                if (
                    not isinstance(kind, str)
                    or not kind.strip()
                    or kind not in _MCP_EVIDENCE_KINDS
                ):
                    raise TaskLifecycleError(
                        "validation_error",
                        "evidence kind cannot be produced by MCP task tools",
                    )
                description = raw["description"]
                if not isinstance(description, str) or not description.strip():
                    raise TaskLifecycleError(
                        "validation_error",
                        "evidence description must be a non-blank string",
                    )
                if not isinstance(raw["required"], bool):
                    raise TaskLifecycleError(
                        "validation_error", "evidence required must be a boolean"
                    )
                evidence.append(EvidenceRequirement.from_mapping(raw))
        contract = GoalContract(
            title=arguments["objective"].strip(),
            summary=arguments.get("summary", ""),
            risk=arguments["risk"],
            permissions=permission,
            evidence_required=evidence,
            verification_requirements=tuple(requirements),
            non_goals=non_goals,
            stopping_policy=dict(stop),
        )
        return self.lifecycle.begin(
            contract,
            idempotency_key=arguments["idempotency_key"],
            workflow=workflow,
        )

    @staticmethod
    def _operation_data(session: TaskSession, operation: str, key: str) -> tuple[dict[str, Any], str]:
        if operation == "reflect":
            reflection = _plain(session.reflection or {})
            return dict(reflection.get("response", {})), str(reflection.get("event_hash", session.latest_event_hash))
        record = session.idempotency[(operation, key)]
        data = _plain(record.response or {})
        event_hash = record.event_hashes[-1]
        if operation == "verify" and isinstance(data.get("event_hash"), str):
            event_hash = data["event_hash"]
        elif operation == "append_evidence" and isinstance(data.get("evidence_hash"), str):
            event_hash = data["evidence_hash"]
        elif operation == "complete" and isinstance(data.get("gate"), dict):
            data = dict(data["gate"])
        return dict(data), event_hash

    @staticmethod
    def _terminal_result(
        session: TaskSession,
        events: list[Any],
    ) -> dict[str, Any] | None:
        if not session.terminal:
            return None
        terminal_transitions = [
            event
            for event in events
            if event.event_type == "state_transition"
            and event.payload.get("state") in {"verified", "rejected"}
        ]
        if not terminal_transitions:
            raise TaskLifecycleError(
                "invalid_task_event",
                "terminal task has no durable terminal transition",
            )
        cause_hash = terminal_transitions[-1].payload.get("cause_event_hash")
        if not isinstance(cause_hash, str) or not cause_hash.strip():
            raise TaskLifecycleError(
                "invalid_task_event",
                "terminal transition has no cause event",
                details={"event_hash": terminal_transitions[-1].entry_hash},
            )
        positions = {
            event_hash: index for index, event_hash in enumerate(session.event_hashes)
        }
        candidates = []
        for record in session.idempotency.values():
            response = record.response if isinstance(record.response, Mapping) else {}
            terminal = False
            if session.state.value == "verified" and record.operation == "complete":
                gate = response.get("gate")
                terminal = isinstance(gate, Mapping) and gate.get("decision") == "pass"
            elif session.state.value == "rejected" and record.operation == "approve":
                terminal = response.get("approved") is False
            elif session.state.value == "rejected" and record.operation == "resolve":
                terminal = response.get("resolution") == "reject"
            if terminal and record.event_hashes and cause_hash in record.event_hashes:
                candidates.append(record)
        if not candidates:
            raise TaskLifecycleError(
                "invalid_task_event",
                "terminal transition lacks a recorded operation result",
                details={
                    "event_hash": terminal_transitions[-1].entry_hash,
                    "cause_event_hash": cause_hash,
                },
            )
        record = max(
            candidates,
            key=lambda item: max(positions.get(value, -1) for value in item.event_hashes),
        )
        data = _plain(record.response or {})
        if record.operation == "complete" and isinstance(data.get("gate"), dict):
            data = data["gate"]
        return {
            "operation": record.operation,
            "event_hash": record.event_hashes[-1],
            "data": data,
        }

    def _resume(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._strict(arguments, allowed={"task_id"}, required={"task_id"})
        task_id = arguments["task_id"]
        if not isinstance(task_id, str) or not task_id.strip():
            raise TaskLifecycleError(
                "invalid_task_id", "task_id must be a non-blank string"
            )
        with self.lifecycle.runtime.execution_lock():
            session = self.lifecycle.get(task_id)
            events = self.ledger.events_for_contract(
                session.task_id, all_segments=True
            )
            contract = GoalContract.from_mapping(_plain(session.contract_snapshot))
            unmet = self.lifecycle.runtime.gate.unmet_verification_ids(
                contract, events
            )
        reflection = _plain(session.reflection or {})
        reflection_result = (
            {
                "event_hash": reflection["event_hash"],
                "data": reflection["response"],
            }
            if reflection
            else None
        )
        return _text_result(
            {
                "ok": True,
                "task": session.to_dict(),
                "data": {
                    "contract": _plain(session.contract_snapshot),
                    "unmet_verification": list(unmet),
                    "pending_intents": [
                        {
                            "kind": intent.kind,
                            "operation": intent.operation,
                            "operation_id": intent.operation_id,
                            "event_hash": intent.event_hash,
                        }
                        for intent in session.unresolved_intents
                    ],
                    "terminal_result": self._terminal_result(session, events),
                    "reflection_result": reflection_result,
                },
            }
        )

    def _lifecycle_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Keep replay classification in the same cross-process critical section
        # as the lifecycle mutation.  Otherwise two servers can both observe
        # the pre-append count and both claim that the winning request was new.
        with self.lifecycle.runtime.execution_lock():
            return self._lifecycle_call_locked(name, arguments)

    def _lifecycle_call_locked(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        operation = name.removeprefix("causality_task_")
        if name in {
            "causality_task_action",
            "causality_task_http",
            "causality_task_browser",
        }:
            operation = "action"
        if name == "causality_append_evidence":
            operation = "append_evidence"
        key = arguments.get("idempotency_key")
        ephemeral: Mapping[str, str] = {}
        before = self.ledger.event_count()
        preexisting_operation = False
        if name == "causality_task_begin":
            session = self._begin(arguments)
        else:
            common = {"task_id", "idempotency_key"}
            if not isinstance(arguments.get("task_id"), str) or not isinstance(key, str):
                raise TaskLifecycleError("validation_error", "task_id and idempotency_key are required")
            task_id = arguments["task_id"]
            if operation != "reflect":
                try:
                    current = self.lifecycle.get(task_id)
                except TaskLifecycleError:
                    current = None
                preexisting_operation = bool(
                    current is not None
                    and (operation, key) in current.idempotency
                )
            if name == "causality_task_phase":
                allowed = common | {"phase_id", "action", "status", "evidence_refs"}
                required = common | {"phase_id", "action"}
                self._strict(arguments, allowed=allowed, required=required)
                phase_action = arguments["action"]
                if phase_action == "start":
                    self._strict(arguments, allowed=required, required=required)
                    status = None
                    refs: tuple[str, ...] = ()
                elif phase_action == "finish":
                    self._strict(
                        arguments,
                        allowed=allowed,
                        required=required | {"status", "evidence_refs"},
                    )
                    status = arguments["status"]
                    if status not in {"passed", "failed", "blocked"}:
                        raise TaskLifecycleError(
                            "validation_error",
                            "phase finish status must be passed, failed, or blocked",
                        )
                    refs = self._string_array(
                        arguments["evidence_refs"], "evidence_refs"
                    )
                else:
                    raise TaskLifecycleError(
                        "validation_error", "phase action must be start or finish"
                    )
                session = self.lifecycle.phase(
                    task_id,
                    phase_id=arguments["phase_id"],
                    action=phase_action,
                    status=status,
                    evidence_refs=refs,
                    idempotency_key=key,
                )
            elif name == "causality_task_hypothesis":
                fields = {
                    "phase_id",
                    "hypothesis",
                    "verifier",
                    "status",
                    "rationale",
                    "evidence_refs",
                }
                self._strict(
                    arguments,
                    allowed=common | fields,
                    required=common | fields,
                )
                if arguments["status"] not in {
                    "supported",
                    "rejected",
                    "inconclusive",
                }:
                    raise TaskLifecycleError(
                        "validation_error",
                        "hypothesis status must be supported, rejected, or inconclusive",
                    )
                session = self.lifecycle.hypothesis(
                    task_id,
                    phase_id=arguments["phase_id"],
                    hypothesis=arguments["hypothesis"],
                    verifier=arguments["verifier"],
                    status=arguments["status"],
                    rationale=arguments["rationale"],
                    evidence_refs=self._string_array(
                        arguments["evidence_refs"], "evidence_refs"
                    ),
                    idempotency_key=key,
                )
            elif name == "causality_task_action":
                self._strict(arguments, allowed=common | {"action"}, required=common | {"action"})
                action = arguments["action"]
                if not isinstance(action, dict):
                    raise TaskLifecycleError("validation_error", "action must be an object")
                kind = action.get("kind")
                fields = {
                    "file_read": ({"kind", "path"}, {"kind", "path"}),
                    "file_write": ({"kind", "path", "content"}, {"kind", "path", "content"}),
                    "subprocess": ({"kind", "argv", "cwd", "timeout_seconds"}, {"kind", "argv"}),
                }
                if kind not in fields:
                    raise TaskLifecycleError("validation_error", "unknown action kind")
                self._strict(action, allowed=fields[kind][0], required=fields[kind][1])
                if kind == "subprocess" and (
                    not isinstance(action.get("argv"), list)
                    or any(not isinstance(item, str) for item in action["argv"])
                ):
                    raise TaskLifecycleError("validation_error", "subprocess.argv must be a string array")
                session = self.lifecycle.action(task_id, action, idempotency_key=key)
            elif name == "causality_task_http":
                fields = {
                    "method",
                    "url",
                    "headers",
                    "body_ref",
                    "timeout_seconds",
                    "expected_statuses",
                    "response_artifact",
                    "auth_ref",
                }
                self._strict(
                    arguments,
                    allowed=common | fields,
                    required=common | {"method", "url", "expected_statuses"},
                )
                action = {
                    "kind": "http",
                    **{field: arguments[field] for field in fields if field in arguments},
                }
                session = self.lifecycle.action(task_id, action, idempotency_key=key)
            elif name == "causality_task_browser":
                browser_operation = arguments.get("operation")
                operation_fields = {
                    "observe": ({"mode", "scope", "annotate"}, set()),
                    "act": (
                        {"action", "ref", "value", "expected_state_hash"},
                        {"action", "ref", "expected_state_hash"},
                    ),
                    "assert": (
                        {"property", "ref", "expected_state_hash"},
                        {"property", "ref", "expected_state_hash"},
                    ),
                    "inspect": (
                        {"inspection", "ref", "expected_state_hash"},
                        {"inspection", "ref", "expected_state_hash"},
                    ),
                    "visual": (
                        {"ref", "expected_state_hash"},
                        {"expected_state_hash"},
                    ),
                }
                if browser_operation not in operation_fields:
                    raise TaskLifecycleError(
                        "validation_error", "unknown browser operation"
                    )
                optional, operation_required = operation_fields[browser_operation]
                self._strict(
                    arguments,
                    allowed=common | {"operation"} | optional,
                    required=common | {"operation"} | operation_required,
                )
                action = {
                    "kind": "browser",
                    **{
                        field: arguments[field]
                        for field in ({"operation"} | optional)
                        if field in arguments
                    },
                }
                receipt = self.lifecycle.perform_action(
                    task_id, action, idempotency_key=key
                )
                session = receipt.session
                ephemeral = receipt.ephemeral
            elif name == "causality_task_verify":
                allowed = common | {"requirement_id", "mode", "evidence_hash", "approved", "approver", "rationale", "proof"}
                self._strict(arguments, allowed=allowed, required=common | {"requirement_id", "mode"})
                mode = arguments["mode"]
                if mode == "manual":
                    self._strict(arguments, allowed=allowed, required=common | {"requirement_id", "mode", "evidence_hash", "approved", "approver", "rationale", "proof"})
                session = self.lifecycle.verify(
                    task_id,
                    arguments["requirement_id"],
                    idempotency_key=key,
                    mode=mode,
                    evidence_hash=arguments.get("evidence_hash"),
                    approved=arguments.get("approved"),
                    approver=arguments.get("approver"),
                    rationale=arguments.get("rationale"),
                    proof=arguments.get("proof"),
                )
            elif name == "causality_task_verdict":
                allowed = common | {"verifier", "status", "rationale", "severity", "evidence_refs"}
                self._strict(arguments, allowed=allowed, required=common | {"verifier", "status", "rationale", "evidence_refs"})
                refs = self._string_array(arguments["evidence_refs"], "evidence_refs")
                session = self.lifecycle.verdict(
                    task_id,
                    verifier=arguments["verifier"],
                    status=arguments["status"],
                    rationale=arguments["rationale"],
                    severity=arguments.get("severity", "normal"),
                    evidence_refs=refs,
                    idempotency_key=key,
                )
            elif name == "causality_task_complete":
                self._strict(arguments, allowed=common, required=common)
                session = self.lifecycle.complete(task_id, idempotency_key=key)
            elif name == "causality_task_approve":
                required = common | {
                    "stage", "approved", "approver", "rationale",
                    "evidence_refs", "proof",
                }
                allowed = required | {"phase_id"}
                self._strict(arguments, allowed=allowed, required=required)
                if arguments["stage"] == "phase" and "phase_id" not in arguments:
                    raise TaskLifecycleError(
                        "validation_error", "phase approval requires phase_id"
                    )
                if arguments["stage"] != "phase" and "phase_id" in arguments:
                    raise TaskLifecycleError(
                        "validation_error",
                        "phase_id is only valid for phase approval",
                    )
                session = self.lifecycle.approve(
                    task_id,
                    stage=arguments["stage"],
                    approved=arguments["approved"],
                    approver=arguments["approver"],
                    rationale=arguments["rationale"],
                    phase_id=arguments.get("phase_id"),
                    evidence_refs=self._string_array(arguments["evidence_refs"], "evidence_refs"),
                    idempotency_key=key,
                    proof=arguments["proof"],
                )
            elif name == "causality_task_resolve":
                allowed = common | {"operation_id", "resolution", "approver", "rationale", "proof"}
                self._strict(arguments, allowed=allowed, required=allowed)
                session = self.lifecycle.resolve(
                    task_id,
                    operation_id=arguments["operation_id"],
                    resolution=arguments["resolution"],
                    approver=arguments["approver"],
                    rationale=arguments["rationale"],
                    idempotency_key=key,
                    proof=arguments["proof"],
                )
            elif name == "causality_task_reflect":
                allowed = common | {"scope", "ttl_days"}
                self._strict(arguments, allowed=allowed, required=common)
                scope = arguments.get("scope")
                if scope is not None and (
                    not isinstance(scope, str) or not scope.strip()
                ):
                    raise TaskLifecycleError(
                        "validation_error",
                        "scope must be a non-blank string",
                    )
                session = self.lifecycle.reflect(
                    task_id,
                    idempotency_key=key,
                    failure_scope=arguments.get("scope"),
                    failure_ttl_days=arguments.get("ttl_days"),
                )
            elif name == "causality_append_evidence":
                allowed = common | {"kind", "payload", "artifact_paths"}
                self._strict(arguments, allowed=allowed, required=common | {"kind", "payload"})
                artifact_paths = (
                    self._string_array(arguments["artifact_paths"], "artifact_paths")
                    if "artifact_paths" in arguments
                    else ()
                )
                session = self.lifecycle.append_evidence(
                    task_id,
                    arguments["kind"],
                    arguments["payload"],
                    artifact_paths=artifact_paths,
                    idempotency_key=key,
                )
            else:
                raise TaskLifecycleError("unknown_tool", f"unknown tool: {name}")
        replayed = (
            self.ledger.event_count() == before
            if operation in {"begin", "reflect"}
            else preexisting_operation
        )
        data, event_hash = self._operation_data(session, operation, str(key))
        if operation == "reflect" and session.state.value == "verified":
            deterministic_id = hashlib.sha256(
                f"{session.task_id}:{event_hash}:skill:v1".encode()
            ).hexdigest()
            try:
                candidate = self.skills.distill_once(
                    self.ledger,
                    self.lifecycle._contract(session),
                    skill_id=deterministic_id,
                    provenance=event_hash,
                    source_task_id=session.task_id,
                )
                data["skill"] = candidate.to_dict()
            except SkillPromotionError as exc:
                raise TaskLifecycleError("skill_distill_failed", str(exc)) from exc
        elif operation == "reflect":
            data["skill"] = None
        if ephemeral:
            data["untrusted"] = {
                name: wrap_untrusted(value) for name, value in ephemeral.items()
            }
        return _text_result(
            {
                "ok": True,
                "task": session.to_dict(),
                "event_hash": event_hash,
                "idempotency": {"key": key, "replayed": replayed},
                "data": data,
            }
        )

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "causality_init":
                cli_only = {"force", "adopt"}.intersection(arguments)
                if cli_only:
                    raise TaskLifecycleError(
                        "validation_error",
                        "CLI-only options require explicit operator action",
                        details={"options": sorted(cli_only)},
                    )
                self._strict(arguments, allowed={"client", "verify"})
                client = arguments.get("client", "auto")
                if not isinstance(client, str) or client not in SUPPORTED_CLIENTS:
                    raise TaskLifecycleError(
                        "validation_error",
                        f"client must be one of: {', '.join(SUPPORTED_CLIENTS)}",
                    )
                verify = arguments.get("verify", False)
                if not isinstance(verify, bool):
                    raise TaskLifecycleError("validation_error", "verify must be a boolean")
                result = install_agent_files(
                    self.project,
                    force=False,
                    client=client,
                    adopt=False,
                    verify=verify,
                )
                return _text_result(json.dumps(result.to_dict(), ensure_ascii=True, indent=2))
            if name == "causality_context":
                self._strict(arguments, allowed={"limit"})
                limit = arguments.get("limit", 5)
                if (
                    isinstance(limit, bool)
                    or not isinstance(limit, int)
                    or limit < 0
                ):
                    raise TaskLifecycleError(
                        "validation_error", "limit must be a non-negative integer"
                    )
                failures = TypedMemory(self.project).entries(
                    "failures", active_only=True
                )
                if limit:
                    failures = failures[-limit:]
                else:
                    failures = []

                def markdown_paths(directory: str) -> list[str]:
                    root = self.project / directory
                    return (
                        sorted(
                            path.relative_to(self.project).as_posix()
                            for path in root.rglob("*.md")
                            if path.is_file()
                        )
                        if root.is_dir()
                        else []
                    )

                with self.lifecycle.runtime.execution_lock():
                    if not self.ledger.verify_chain():
                        raise TaskLifecycleError(
                            "ledger_integrity_failed",
                            "ledger hash chain verification failed",
                        )
                    tail = self.ledger.context_tail(limit)

                return _text_result(
                    json.dumps(
                        {
                            "ok": True,
                            "project": str(self.project),
                            "ledger_tail": tail,
                            "workflows": [item["name"] for item in workflow_manifest()["workflows"]],
                            "knowledge": {
                                "active_failures": [
                                    item.to_dict() for item in failures
                                ],
                                "curated_markdown": {
                                    "memory": markdown_paths("memory"),
                                    "skills": markdown_paths("skills"),
                                },
                                "runtime_jsonl": {
                                    "classification": "local_runtime",
                                    "recommended_ignore_patterns": [
                                        "memory/**/*.jsonl",
                                        "skills/**/*.jsonl",
                                    ],
                                },
                            },
                        },
                        ensure_ascii=True,
                        indent=2,
                    )
                )
            if name == "causality_workflows":
                self._strict(arguments, allowed=set())
                return _text_result(json.dumps(workflow_manifest(), ensure_ascii=True, indent=2))
            if name == "causality_skill_outcome":
                self._strict(arguments, allowed={"task_id", "idempotency_key", "skill_id", "success", "evidence_refs"}, required={"task_id", "idempotency_key", "skill_id", "success", "evidence_refs"})
                task_id = arguments["task_id"]
                session = self.lifecycle.get(task_id)
                if not session.terminal or session.state.value != "verified":
                    raise TaskLifecycleError("validation_error", "skill outcome requires a verified terminal task")
                refs = self._string_array(arguments["evidence_refs"], "evidence_refs")
                if len(refs) != len(set(refs)) or any(ref not in session.event_hashes for ref in refs):
                    raise TaskLifecycleError("validation_error", "evidence_refs must reference task event hashes")
                candidate = self.skills.record_outcome(arguments["skill_id"], success=arguments["success"], attempt_id=task_id, evidence_refs=refs)
                return _text_result({"ok": True, "skill": candidate.to_dict(), "idempotency": {"key": arguments["idempotency_key"]}})
            if name == "causality_skill_promote":
                self._strict(arguments, allowed={"skill_id", "idempotency_key", "approved_by", "evidence_refs", "proof"}, required={"skill_id", "idempotency_key", "approved_by", "evidence_refs", "proof"})
                refs = self._string_array(arguments["evidence_refs"], "evidence_refs")
                if len(refs) != len(set(refs)) or any(not isinstance(ref, str) or len(ref) != 64 for ref in refs):
                    raise TaskLifecycleError("validation_error", "evidence_refs must be unique SHA-256 hashes")
                if not self._authorize(arguments["approved_by"], "skill", arguments["proof"]):
                    raise TaskLifecycleError("approval_required", "skill promotion requires authenticated approval")
                authored = [p.stem for root in (self.project / "skills", self.project / "workflow") if root.is_dir() for p in root.rglob("*.md") if p.name.lower() != "readme.md"]
                candidate = self.skills.promote(arguments["skill_id"], approved_by=arguments["approved_by"], authored_names=authored, min_successes=2, min_attempts=3, dedup_threshold=0.6, evidence_refs=refs)
                return _text_result({"ok": True, "skill": candidate.to_dict(), "idempotency": {"key": arguments["idempotency_key"]}, "gate": {"min_successes": 2, "min_attempts": 3, "dedup_threshold": 0.6}})
            if name == "causality_task_resume":
                return self._resume(arguments)
            lifecycle_names = {
                "causality_task_begin", "causality_task_approve", "causality_task_action",
                "causality_task_phase", "causality_task_hypothesis",
                "causality_task_http", "causality_task_browser",
                "causality_task_verify", "causality_task_verdict", "causality_task_complete",
                "causality_task_resolve", "causality_task_reflect", "causality_append_evidence",
            }
            if name in lifecycle_names:
                return self._lifecycle_call(name, arguments)
            raise TaskLifecycleError("unknown_tool", f"unknown tool: {name}")
        except TaskLifecycleError as exc:
            task: dict[str, Any] | None = None
            task_id = arguments.get("task_id")
            if isinstance(task_id, str):
                try:
                    task = self.lifecycle.get(task_id).to_dict()
                except TaskLifecycleError:
                    pass
            payload: dict[str, Any] = {"ok": False, "error": exc.to_dict()}
            if task is not None:
                payload["task"] = task
            return _text_result(payload, is_error=True)
        except (KeyError, TypeError, ValueError) as exc:
            return _text_result(
                {
                    "ok": False,
                    "error": {
                        "code": "validation_error",
                        "message": str(exc),
                        "retryable": False,
                        "details": {},
                    },
                },
                is_error=True,
            )

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(project: str | Path = ".") -> int:
    server = CausalityMCPServer(project)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = server._error(None, -32700, f"parse error: {exc.msg}")
        else:
            response = server.handle(request)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
        sys.stdout.flush()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Causality MCP-style stdio server")
    parser.add_argument("--project", default=".")
    args = parser.parse_args()
    return serve(args.project)


if __name__ == "__main__":
    raise SystemExit(main())
