"""Reference client state machine for Spec 007 automatic orchestration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import uuid4

from .contracts import utc_now
from .orchestration_checkpoint import (
    CheckpointStore,
    OrchestrationCheckpoint,
    OrchestrationError,
    semantic_request_sha256,
)
from .task_lifecycle import canonical_sha256


class OrchestrationTransport(Protocol):
    def tools(self) -> tuple[str, ...]: ...

    def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...




@dataclass(frozen=True)
class DriverDirective:
    kind: str
    reason: str
    task_id: str | None = None
    tool: str | None = None
    operation: str | None = None
    details: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "task_id": self.task_id,
            "tool": self.tool,
            "operation": self.operation,
            "details": dict(self.details or {}),
        }


class InProcessMCPTransport:
    """Small adapter used by tests and embedded clients."""

    def __init__(self, server: Any):
        self.server = server
        self._request_id = 0

    def _handle(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self._request_id += 1
        request: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
        }
        if params is not None:
            request["params"] = dict(params)
        response = self.server.handle(request)
        if response is None or "error" in response:
            raise OrchestrationError("MCP transport failed")
        return response["result"]

    def tools(self) -> tuple[str, ...]:
        result = self._handle("tools/list")
        return tuple(tool["name"] for tool in result["tools"])

    def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self._handle(
            "tools/call", {"name": name, "arguments": dict(arguments)}
        )
        content = result.get("content", ())
        if not content:
            raise OrchestrationError("MCP tool returned no content")
        value = json.loads(content[0]["text"])
        if not isinstance(value, dict):
            raise OrchestrationError("MCP tool payload must be an object")
        return value


class ReferenceOrchestrator:
    """One-step-at-a-time driver; host, human, and verifiers retain judgment."""

    def __init__(
        self,
        transport: OrchestrationTransport,
        checkpoints: CheckpointStore,
        *,
        lease_seconds: int = 60,
    ):
        self.transport = transport
        self.checkpoints = checkpoints
        self.controller_id = checkpoints.controller_id
        self.lease_seconds = lease_seconds
        self._tools: set[str] = set()
        self._active = False

    def bootstrap(self, client: str = "auto") -> DriverDirective:
        self._tools = set(self.transport.tools())
        if "causality_init" not in self._tools:
            return DriverDirective("capability_unavailable", "causality_init is not advertised")
        result = self.transport.call(
            "causality_init", {"client": client, "verify": True}
        )
        activation = result.get("activation")
        project_root = result.get("project_root")
        project_matches = (
            isinstance(project_root, str)
            and Path(project_root).resolve() == self.checkpoints.project
        )
        if activation != "active" or not project_matches:
            return DriverDirective(
                "bootstrap_blocked",
                (
                    f"Causality activation is {activation or 'unknown'}"
                    if activation != "active"
                    else "Causality MCP project does not match the checkpoint project"
                ),
                details={"remediation": list(result.get("remediation", ()))},
            )
        self._active = True
        return DriverDirective("ready", "Causality activation and tool discovery passed")

    def _activation_issue(self) -> DriverDirective | None:
        if self._active:
            return None
        return DriverDirective(
            "bootstrap_required",
            "bootstrap must prove active installation and matching project before work",
        )

    def _available(self) -> set[str]:
        if not self._tools:
            self._tools = set(self.transport.tools())
        return self._tools

    def begin(self, contract: Mapping[str, Any]) -> Mapping[str, Any] | DriverDirective:
        """Begin one task with a deterministic key, then claim its controller lease."""

        issue = self._activation_issue()
        if issue is not None:
            return issue
        if "causality_task_begin" not in self._available():
            return DriverDirective(
                "capability_unavailable", "task begin is not advertised"
            )
        arguments = dict(contract)
        discriminator = canonical_sha256(arguments)
        arguments.setdefault("idempotency_key", self._key("new", "begin", discriminator))
        result = self._call_mutation(
            "causality_task_begin",
            arguments,
            task_id=None,
            lease_id=None,
        )
        if isinstance(result, DriverDirective):
            return result
        task = result.get("task")
        if not isinstance(task, dict) or not isinstance(task.get("task_id"), str):
            return DriverDirective("recovery_required", "task begin returned no task id")
        claimed = self.claim(
            task["task_id"], last_event_hash=task.get("latest_event_hash")
        )
        if isinstance(claimed, DriverDirective):
            return claimed
        return {**result, "lease": claimed.get("lease")}

    @staticmethod
    def _key(task_id: str, operation: str, discriminator: str = "") -> str:
        digest = hashlib.sha256(
            f"{task_id}:{operation}:{discriminator}".encode("utf-8")
        ).hexdigest()[:32]
        return f"orch:{operation}:{digest}"

    def _checkpoint(
        self,
        *,
        operation: str,
        arguments: Mapping[str, Any],
        status: str,
        task_id: str | None,
        lease_id: str | None,
        phase_id: str | None,
        last_event_hash: str | None,
    ) -> OrchestrationCheckpoint:
        checkpoint = OrchestrationCheckpoint(
            controller_id=self.controller_id,
            operation=operation,
            idempotency_key=str(arguments.get("idempotency_key", "bootstrap")),
            request_sha256=semantic_request_sha256(operation, arguments),
            status=status,
            task_id=task_id,
            lease_id=lease_id,
            phase_id=phase_id,
            last_event_hash=last_event_hash,
            updated_at=utc_now(),
        )
        self.checkpoints.save(checkpoint)
        return checkpoint

    def _call_mutation(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        task_id: str | None,
        lease_id: str | None,
        phase_id: str | None = None,
        proof_bearing: bool = False,
        last_event_hash: str | None = None,
    ) -> Mapping[str, Any] | DriverDirective:
        with self.checkpoints.transaction():
            return self._call_mutation_locked(
                name,
                arguments,
                task_id=task_id,
                lease_id=lease_id,
                phase_id=phase_id,
                proof_bearing=proof_bearing,
                last_event_hash=last_event_hash,
            )

    def _call_mutation_locked(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        task_id: str | None,
        lease_id: str | None,
        phase_id: str | None,
        proof_bearing: bool,
        last_event_hash: str | None,
    ) -> Mapping[str, Any] | DriverDirective:
        prepared = self.checkpoints.load()
        if prepared is not None and prepared.status == "human_required":
            return DriverDirective(
                "human_input_required",
                "an uncertain proof-bearing mutation requires explicit operator recovery",
                task_id=task_id,
                tool=prepared.operation,
            )
        if prepared is not None and prepared.status == "prepared":
            if prepared.operation != name or prepared.task_id != task_id:
                return DriverDirective(
                    "recovery_required",
                    "a different checkpointed mutation is still uncertain",
                    task_id=task_id,
                    tool=prepared.operation,
                )
            arguments["idempotency_key"] = prepared.idempotency_key
            if semantic_request_sha256(name, arguments) != prepared.request_sha256:
                return DriverDirective(
                    "recovery_required",
                    "the reconstructed request does not match the prepared checkpoint",
                    task_id=task_id,
                    tool=name,
                )
        self._checkpoint(
            operation=name,
            arguments=arguments,
            status="prepared",
            task_id=task_id,
            lease_id=lease_id,
            phase_id=phase_id,
            last_event_hash=last_event_hash,
        )
        try:
            result = self.transport.call(name, arguments)
        except Exception:
            status = "human_required" if proof_bearing else "prepared"
            self._checkpoint(
                operation=name,
                arguments=arguments,
                status=status,
                task_id=task_id,
                lease_id=lease_id,
                phase_id=phase_id,
                last_event_hash=last_event_hash,
            )
            return DriverDirective(
                "human_input_required" if proof_bearing else "recovery_required",
                "the mutation response is uncertain; do not guess or replay an effect",
                task_id=task_id,
                tool=name,
            )
        event_hash = result.get("event_hash")
        task = result.get("task") if isinstance(result.get("task"), dict) else {}
        if result.get("ok") is not True:
            self._checkpoint(
                operation=name,
                arguments=arguments,
                status="acknowledged",
                task_id=task_id or task.get("task_id"),
                lease_id=lease_id,
                phase_id=phase_id,
                last_event_hash=task.get("latest_event_hash"),
            )
            return DriverDirective(
                "blocked",
                str(result.get("error", {}).get("code", "mutation_failed")),
                task_id=task_id,
                tool=name,
                details={"error": dict(result.get("error", {}))},
            )
        self._checkpoint(
            operation=name,
            arguments=arguments,
            status="acknowledged",
            task_id=task_id or task.get("task_id"),
            lease_id=lease_id,
            phase_id=phase_id,
            last_event_hash=event_hash or task.get("latest_event_hash"),
        )
        return result

    def claim(
        self, task_id: str, *, last_event_hash: str | None
    ) -> Mapping[str, Any] | DriverDirective:
        if "causality_task_lease" not in self._available():
            return DriverDirective(
                "capability_unavailable", "controller lease is not advertised", task_id
            )
        key = self._key(task_id, "lease", f"{self.controller_id}:{uuid4()}")
        return self._call_mutation(
            "causality_task_lease",
            {
                "task_id": task_id,
                "controller_id": self.controller_id,
                "action": "acquire",
                "ttl_seconds": self.lease_seconds,
                "idempotency_key": key,
            },
            task_id=task_id,
            lease_id=None,
            last_event_hash=last_event_hash,
        )

    def release(
        self, task_id: str, lease_id: str, *, last_event_hash: str | None
    ) -> Mapping[str, Any] | DriverDirective:
        return self._call_mutation(
            "causality_task_lease",
            {
                "task_id": task_id,
                "controller_id": self.controller_id,
                "action": "release",
                "lease_id": lease_id,
                "idempotency_key": self._key(task_id, "release", lease_id),
            },
            task_id=task_id,
            lease_id=lease_id,
            last_event_hash=last_event_hash,
        )

    def _resume(self, task_id: str) -> Mapping[str, Any] | DriverDirective:
        issue = self._activation_issue()
        if issue is not None:
            return issue
        if "causality_task_resume" not in self._available():
            return DriverDirective(
                "capability_unavailable", "task resume is not advertised", task_id
            )
        result = self.transport.call("causality_task_resume", {"task_id": task_id})
        if result.get("ok") is not True:
            return DriverDirective("blocked", "task resume failed", task_id)
        checkpoint = self.checkpoints.load()
        task = result.get("task")
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        lease = data.get("controller_lease") if isinstance(data, dict) else None
        if (
            checkpoint is not None
            and checkpoint.status == "prepared"
            and checkpoint.operation == "causality_task_lease"
            and checkpoint.task_id == task_id
        ):
            if checkpoint.lease_id is None:
                lease_arguments = {
                    "task_id": task_id,
                    "controller_id": self.controller_id,
                    "action": "acquire",
                    "ttl_seconds": self.lease_seconds,
                    "idempotency_key": checkpoint.idempotency_key,
                }
            else:
                lease_arguments = {
                    "task_id": task_id,
                    "controller_id": self.controller_id,
                    "action": "release",
                    "lease_id": checkpoint.lease_id,
                    "idempotency_key": checkpoint.idempotency_key,
                }
            if (
                semantic_request_sha256("causality_task_lease", lease_arguments)
                != checkpoint.request_sha256
            ):
                return DriverDirective(
                    "recovery_required",
                    "the prepared lease request cannot be reconstructed exactly",
                    task_id,
                    "causality_task_lease",
                )
            replayed = self._call_mutation(
                "causality_task_lease",
                lease_arguments,
                task_id=task_id,
                lease_id=checkpoint.lease_id,
                last_event_hash=checkpoint.last_event_hash,
            )
            if isinstance(replayed, DriverDirective):
                return replayed
            checkpoint = self.checkpoints.load()
            result = self.transport.call(
                "causality_task_resume", {"task_id": task_id}
            )
            if result.get("ok") is not True or not isinstance(result.get("task"), dict):
                return DriverDirective(
                    "recovery_required",
                    "task resume failed after replaying the prepared lease",
                    task_id,
                )
            task = result["task"]
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            lease = data.get("controller_lease") if isinstance(data, dict) else None
        task_advanced = bool(
            checkpoint is not None
            and isinstance(task, dict)
            and isinstance(task.get("latest_event_hash"), str)
            and task.get("latest_event_hash") != checkpoint.last_event_hash
        )
        lease_applied = bool(
            checkpoint is not None
            and checkpoint.operation == "causality_task_lease"
            and isinstance(lease, dict)
            and lease.get("controller_id") == self.controller_id
            and lease.get("status") in {"active", "released"}
        )
        if (
            checkpoint is not None
            and checkpoint.status in {"prepared", "human_required"}
            and checkpoint.task_id == task_id
            and isinstance(task, dict)
            and (task_advanced or lease_applied)
        ):
            updated = replace(
                checkpoint,
                status="acknowledged",
                last_event_hash=task.get("latest_event_hash"),
                updated_at=utc_now(),
            )
            try:
                self.checkpoints.compare_and_save(checkpoint, updated)
            except OrchestrationError:
                return DriverDirective(
                    "recovery_required", "checkpoint changed during resume", task_id
                )
        return result

    def advance(self, task_id: str, *, max_steps: int = 32) -> DriverDirective:
        """Run deterministic transitions until judgment or external work is needed."""

        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
            raise OrchestrationError("max_steps must be a positive integer")
        for _ in range(max_steps):
            directive = self.step(task_id)
            if directive.kind != "advanced":
                return directive
        return DriverDirective(
            "bounded_stop",
            "automatic transition budget was exhausted",
            task_id,
        )

    def _ensure_owned_lease(
        self, task_id: str, resumed: Mapping[str, Any]
    ) -> Mapping[str, Any] | DriverDirective:
        task = resumed["task"]
        lease = resumed["data"].get("controller_lease")
        if lease and lease.get("status") == "active":
            if lease.get("controller_id") != self.controller_id:
                return DriverDirective(
                    "controller_conflict", "another controller owns the task", task_id
                )
            return lease
        claimed = self.claim(
            task_id, last_event_hash=task.get("latest_event_hash")
        )
        if isinstance(claimed, DriverDirective):
            return claimed
        return claimed["lease"]

    @staticmethod
    def _durable_verification_failure(
        task: Mapping[str, Any], recommendation: Mapping[str, Any]
    ) -> DriverDirective | None:
        operation = recommendation.get("operation")
        if operation not in {"verify", "approve"}:
            return None
        results = task.get("requirement_results")
        if not isinstance(results, Mapping):
            return None
        if operation == "verify":
            requirement_ids = (recommendation.get("requirement_id"),)
        else:
            evidence_refs = set(recommendation.get("evidence_refs", ()))
            matched = tuple(
                requirement_id
                for requirement_id, result in results.items()
                if isinstance(result, Mapping)
                and result.get("evidence_event_hash") in evidence_refs
            )
            failed_ids = tuple(
                requirement_id
                for requirement_id, result in results.items()
                if isinstance(result, Mapping)
                and result.get("status") in {"fail", "blocked", "timeout", "error"}
            )
            requirement_ids = matched or (failed_ids if len(failed_ids) == 1 else ())
        requirement_id = next((item for item in requirement_ids if item in results), None)
        verification = results.get(requirement_id)
        if not isinstance(verification, Mapping) or verification.get("status") not in {
            "fail", "blocked", "timeout", "error",
        }:
            return None
        return DriverDirective(
            "verification_failed",
            "verification did not pass; explicit host recovery is required",
            task.get("task_id"),
            "causality_task_verify",
            "verify",
            details={
                "requirement_id": requirement_id,
                "status": verification.get("status"),
                "evidence_event_hash": verification.get("evidence_event_hash"),
            },
        )

    def step(self, task_id: str) -> DriverDirective:
        resumed = self._resume(task_id)
        if isinstance(resumed, DriverDirective):
            return resumed
        task = resumed["task"]
        lease = resumed["data"].get("controller_lease")
        recommendation = task["recommended_next"]
        operation = recommendation["operation"]
        failed = self._durable_verification_failure(task, recommendation)
        if failed is not None:
            return failed
        if operation == "done":
            if (
                lease
                and lease.get("status") == "active"
                and lease.get("controller_id") == self.controller_id
            ):
                released = self.release(
                    task_id,
                    lease["lease_id"],
                    last_event_hash=task.get("latest_event_hash"),
                )
                if isinstance(released, DriverDirective):
                    return released
            return DriverDirective(
                "terminal", recommendation["reason"], task_id,
                operation=operation,
                details={"event_hash": task["latest_event_hash"]},
            )
        if not lease or lease.get("status") != "active":
            claimed = self.claim(
                task_id, last_event_hash=task.get("latest_event_hash")
            )
            if isinstance(claimed, DriverDirective):
                return claimed
            lease = claimed["lease"]
            resumed = self._resume(task_id)
            if isinstance(resumed, DriverDirective):
                return resumed
            task = resumed["task"]
        if lease.get("controller_id") != self.controller_id:
            return DriverDirective(
                "controller_conflict", "another controller owns the task", task_id
            )
        lease_id = lease["lease_id"]
        tool = recommendation.get("tool")
        if tool not in self._available():
            return DriverDirective(
                "capability_unavailable",
                f"{tool or operation} is not advertised",
                task_id,
                tool,
                operation,
            )
        if operation in {"action", "append_evidence", "hypothesis"}:
            return DriverDirective(
                "host_action_required",
                recommendation["reason"],
                task_id,
                tool,
                operation,
                details={"phase_id": recommendation.get("phase_id")},
            )
        if operation == "verdict":
            return DriverDirective(
                "verifier_required",
                recommendation["reason"],
                task_id,
                tool,
                operation,
                details={
                    "phase_id": recommendation.get("phase_id"),
                    "evidence_refs": list(recommendation.get("evidence_refs", ())),
                },
            )
        if recommendation.get("requires_human"):
            return DriverDirective(
                "human_input_required",
                recommendation["reason"],
                task_id,
                tool,
                operation,
                details={
                    key: recommendation[key]
                    for key in (
                        "approval_stage",
                        "phase_id",
                        "operation_id",
                        "requirement_id",
                        "evidence_refs",
                    )
                    if key in recommendation
                },
            )
        phase_id = recommendation.get("phase_id")
        discriminator = ":".join(
            str(value)
            for value in (
                phase_id,
                recommendation.get("requirement_id"),
                task.get("latest_event_hash"),
            )
            if value
        )
        arguments: dict[str, Any] = {
            "task_id": task_id,
            "controller_id": self.controller_id,
            "lease_id": lease_id,
            "idempotency_key": self._key(task_id, operation, discriminator),
        }
        if operation == "phase_start":
            arguments.update({"phase_id": phase_id, "action": "start"})
        elif operation == "phase_finish":
            arguments.update(
                {
                    "phase_id": phase_id,
                    "action": "finish",
                    "status": "passed",
                    "evidence_refs": list(recommendation.get("evidence_refs", ())),
                }
            )
        elif operation == "verify":
            arguments.update(
                {
                    "requirement_id": recommendation["requirement_id"],
                    "mode": "execute",
                }
            )
        elif operation not in {"complete", "reflect"}:
            return DriverDirective(
                "recovery_required", "unsupported deterministic transition", task_id
            )
        result = self._call_mutation(
            tool,
            arguments,
            task_id=task_id,
            lease_id=lease_id,
            phase_id=phase_id,
            last_event_hash=task.get("latest_event_hash"),
        )
        if isinstance(result, DriverDirective):
            return result
        if operation == "verify":
            result_task = result.get("task")
            if not isinstance(result_task, Mapping):
                return DriverDirective(
                    "recovery_required", "verification result is missing", task_id,
                    tool, operation,
                )
            failed = self._durable_verification_failure(result_task, recommendation)
            if failed is not None:
                return failed
        return DriverDirective(
            "advanced",
            f"{operation} was durably acknowledged",
            task_id,
            tool,
            operation,
            details={"event_hash": result.get("event_hash")},
        )

    def submit_host_action(
        self,
        task_id: str,
        arguments: Mapping[str, Any],
    ) -> DriverDirective:
        """Checkpoint one host-selected action without inventing its judgment."""

        resumed = self._resume(task_id)
        if isinstance(resumed, DriverDirective):
            return resumed
        task = resumed["task"]
        recommendation = task["recommended_next"]
        operation = recommendation.get("operation")
        tool = recommendation.get("tool")
        if operation not in {"action", "append_evidence", "hypothesis"}:
            return DriverDirective("blocked", "task is not waiting for host work", task_id)
        if tool not in self._available():
            return DriverDirective(
                "capability_unavailable", f"{tool or operation} is not advertised", task_id
            )
        lease = self._ensure_owned_lease(task_id, resumed)
        if isinstance(lease, DriverDirective):
            return lease
        payload = dict(arguments)
        payload.update(
            {
                "task_id": task_id,
                "controller_id": self.controller_id,
                "lease_id": lease.get("lease_id"),
            }
        )
        phase_id = recommendation.get("phase_id")
        if phase_id is not None:
            payload.setdefault("phase_id", phase_id)
        payload.setdefault(
            "idempotency_key",
            self._key(task_id, operation, task["latest_event_hash"]),
        )
        result = self._call_mutation(
            tool,
            payload,
            task_id=task_id,
            lease_id=lease.get("lease_id"),
            phase_id=phase_id,
            last_event_hash=task.get("latest_event_hash"),
        )
        if isinstance(result, DriverDirective):
            return result
        return DriverDirective(
            "advanced",
            "host action was durably acknowledged",
            task_id,
            tool,
            operation,
            details={"event_hash": result.get("event_hash")},
        )

    def submit_human(
        self, task_id: str, arguments: Mapping[str, Any], *, proof: str,
    ) -> DriverDirective:
        """Submit explicit operator judgment without persisting its proof."""

        resumed = self._resume(task_id)
        if isinstance(resumed, DriverDirective):
            return resumed
        task = resumed["task"]
        recommendation = task["recommended_next"]
        tool = recommendation.get("tool")
        if not recommendation.get("requires_human") or tool not in {
            "causality_task_approve", "causality_task_resolve",
            "causality_task_verify",
        }:
            return DriverDirective("blocked", "task is not waiting for HITL", task_id)
        lease = self._ensure_owned_lease(task_id, resumed)
        if isinstance(lease, DriverDirective):
            return lease
        payload = dict(arguments)
        if tool == "causality_task_verify":
            requirement_id = recommendation.get("requirement_id")
            if payload.get("requirement_id", requirement_id) != requirement_id or payload.get(
                "mode", "manual"
            ) != "manual":
                return DriverDirective(
                    "blocked", "human decision does not match the recommended verification", task_id
                )
            payload["requirement_id"] = requirement_id
            payload["mode"] = "manual"
        payload.update({
            "task_id": task_id, "controller_id": self.controller_id,
            "lease_id": lease.get("lease_id"), "proof": proof,
        })
        payload.setdefault(
            "idempotency_key",
            self._key(task_id, recommendation["operation"], task["latest_event_hash"]),
        )
        result = self._call_mutation(
            tool, payload, task_id=task_id, lease_id=lease.get("lease_id"),
            phase_id=recommendation.get("phase_id"), proof_bearing=True,
            last_event_hash=task.get("latest_event_hash"),
        )
        if isinstance(result, DriverDirective):
            return result
        return DriverDirective(
            "advanced", "human decision was durably acknowledged", task_id, tool
        )

    def submit_verifier(
        self, task_id: str, *, verifier_id: str, provider_id: str,
        status: str, rationale: str, evidence_refs: tuple[str, ...],
    ) -> DriverDirective:
        """Submit one provider-attributed verdict over the exact current evidence."""

        resumed = self._resume(task_id)
        if isinstance(resumed, DriverDirective):
            return resumed
        task = resumed["task"]
        recommendation = task["recommended_next"]
        if recommendation.get("operation") != "verdict":
            return DriverDirective("blocked", "task is not waiting for a verifier", task_id)
        expected = tuple(recommendation.get("evidence_refs", ()))
        if not verifier_id.strip() or not provider_id.strip() or set(evidence_refs) != set(expected):
            return DriverDirective("blocked", "verifier handoff is incomplete", task_id)
        lease = self._ensure_owned_lease(task_id, resumed)
        if isinstance(lease, DriverDirective):
            return lease
        arguments = {
            "task_id": task_id, "controller_id": self.controller_id,
            "lease_id": lease.get("lease_id"), "verifier": verifier_id,
            "provider_id": provider_id, "status": status, "rationale": rationale,
            "evidence_refs": list(evidence_refs),
            "idempotency_key": self._key(
                task_id, "verdict",
                canonical_sha256({
                    "provider_id": provider_id,
                    "verifier": verifier_id,
                    "evidence_refs": sorted(evidence_refs),
                }),
            ),
        }
        result = self._call_mutation(
            "causality_task_verdict", arguments, task_id=task_id,
            lease_id=lease.get("lease_id"),
            phase_id=recommendation.get("phase_id"),
            last_event_hash=task.get("latest_event_hash"),
        )
        if isinstance(result, DriverDirective):
            return result
        return DriverDirective(
            "advanced", "verifier decision was durably acknowledged", task_id,
            "causality_task_verdict", "verdict",
        )

__all__ = [
    "CheckpointStore",
    "DriverDirective",
    "InProcessMCPTransport",
    "OrchestrationCheckpoint",
    "OrchestrationError",
    "OrchestrationTransport",
    "ReferenceOrchestrator",
    "semantic_request_sha256",
]
