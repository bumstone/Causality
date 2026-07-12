from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .contracts import (
    AuditEventType,
    EvidenceKind,
    GoalContract,
    StateTransition,
    VerificationResult,
    VerifierDecision,
    contract_binding_payload,
)
from .durable import file_lock
from .gates import GateResult, HITLGate
from .ledger import EvidenceLedger, LedgerEvent


def _event_payload(
    value: dict[str, Any],
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if metadata:
        overlap = value.keys() & metadata.keys()
        if overlap:
            raise ValueError(
                "event metadata cannot replace authoritative fields: "
                + ", ".join(sorted(overlap))
            )
        value.update(metadata)
    return value


class Causality:
    """Small orchestration facade for embedding in an Causality runtime."""

    def __init__(
        self,
        ledger_path: str | Path,
        *,
        project_root: str | Path | None = None,
    ):
        resolved_ledger = Path(ledger_path).resolve()
        if project_root is None:
            parent = resolved_ledger.parent
            project_root = parent.parent if parent.name == ".causality" else parent
        self.project_root = Path(project_root).resolve()
        self.ledger = EvidenceLedger(ledger_path)
        self.gate = HITLGate(self.ledger)

    def execution_lock(self) -> AbstractContextManager[None]:
        """Serialize gated actions, verification, and completion on this ledger."""
        return file_lock(self.ledger.path)

    def create_contract(self, contract: GoalContract) -> GoalContract:
        with self.execution_lock():
            if not self.ledger.verify_chain():
                raise RuntimeError("ledger hash chain verification failed")
            if (
                contract.workspace_root
                and Path(contract.workspace_root).resolve() != self.project_root
            ):
                raise ValueError("contract workspace_root differs from runtime project root")
            for requirement in contract.verification_requirements:
                for declared_path in requirement.artifact_paths:
                    candidate = Path(declared_path)
                    if not candidate.is_absolute():
                        candidate = self.project_root / candidate
                    if not candidate.resolve().is_relative_to(self.project_root):
                        raise ValueError(
                            "verification artifact path escapes project root: "
                            f"{declared_path}"
                        )
            contract.workspace_root = str(self.project_root)
            existing_payload = self.ledger.contract_snapshot(contract.goal_id)
            if existing_payload is not None:
                existing = GoalContract.from_mapping(existing_payload)
                if contract_binding_payload(contract) == contract_binding_payload(existing):
                    return existing
                raise ValueError(f"contract goal_id already exists: {contract.goal_id}")
            self.ledger.append(
                AuditEventType.GOAL_CONTRACT,
                contract.to_dict(),
                contract_id=contract.goal_id,
            )
            return contract

    def frozen_contract(self, contract: GoalContract) -> GoalContract:
        from .verification import snapshot_contract

        return snapshot_contract(self, contract)

    def transition(
        self,
        contract: GoalContract,
        state: StateTransition | str,
        reason: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> LedgerEvent:
        try:
            state_value = StateTransition(state).value
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown task state: {state!r}") from exc
        with self.execution_lock():
            self.frozen_contract(contract)
            event = self.ledger.append(
                AuditEventType.STATE_TRANSITION,
                _event_payload(
                    {
                        "schema_version": 1,
                        "task_id": contract.goal_id,
                        "from_state": contract.state_value,
                        "state": state_value,
                        "reason": reason,
                    },
                    metadata,
                ),
                contract_id=contract.goal_id,
            )
            contract.state = state_value
            return event

    def approve(
        self,
        contract: GoalContract,
        stage: str,
        approver: str,
        rationale: str,
        *,
        evidence_refs: Iterable[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> LedgerEvent:
        if not isinstance(approver, str) or not approver.strip():
            raise ValueError("approver must be a non-blank string")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError("approval rationale must be a non-blank string")
        with self.execution_lock():
            self.frozen_contract(contract)
            return self.ledger.append(
                AuditEventType.HUMAN_DECISION,
                _event_payload(
                    {
                        "stage": stage,
                        "approved": True,
                        "approver": approver.strip(),
                        "rationale": rationale.strip(),
                        "evidence_refs": list(evidence_refs),
                    },
                    metadata,
                ),
                contract_id=contract.goal_id,
            )

    def reject(
        self,
        contract: GoalContract,
        stage: str,
        approver: str,
        rationale: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> LedgerEvent:
        if not isinstance(approver, str) or not approver.strip():
            raise ValueError("approver must be a non-blank string")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError("rejection rationale must be a non-blank string")
        with self.execution_lock():
            self.frozen_contract(contract)
            return self.ledger.append(
                AuditEventType.HUMAN_DECISION,
                _event_payload(
                    {
                        "stage": stage,
                        "approved": False,
                        "approver": approver.strip(),
                        "rationale": rationale.strip(),
                    },
                    metadata,
                ),
                contract_id=contract.goal_id,
            )

    def record_evidence(
        self,
        contract: GoalContract,
        kind: EvidenceKind | str,
        payload: dict[str, Any],
        artifact_paths: Iterable[str | Path] = (),
    ) -> LedgerEvent:
        from .verification import workspace_fingerprint, workspace_fingerprint_digest

        with self.execution_lock():
            self.frozen_contract(contract)
            workspace_digest = workspace_fingerprint_digest(
                workspace_fingerprint(self.project_root, self.ledger.path)
            )
            kind_value = kind.value if isinstance(kind, EvidenceKind) else str(kind)
            event_payload = {
                **payload,
                "kind": kind_value,
                "evidence_workspace_fingerprint_sha256": workspace_digest,
            }
            event_payload.setdefault("workspace_fingerprint_sha256", workspace_digest)
            return self.ledger.append(
                AuditEventType.EVIDENCE,
                event_payload,
                contract_id=contract.goal_id,
                artifact_paths=artifact_paths,
            )

    def verify_requirement(
        self,
        contract: GoalContract,
        requirement_id: str,
        *,
        root: str | Path | None = None,
        before_effect: Callable[[], None] | None = None,
        transition_on_failure: bool = True,
    ) -> VerificationResult:
        from .verification import execute_requirement

        return execute_requirement(
            self,
            contract,
            requirement_id,
            root=root,
            before_effect=before_effect,
            transition_on_failure=transition_on_failure,
        )

    def record_manual_verification(
        self,
        contract: GoalContract,
        requirement_id: str,
        *,
        evidence_hash: str,
        approved: bool,
        approver: str,
        rationale: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> LedgerEvent:
        from .verification import (
            find_requirement,
            workspace_fingerprint,
            workspace_fingerprint_digest,
        )

        with self.execution_lock():
            requirement = find_requirement(self, contract, requirement_id)
            if not requirement.manual:
                raise ValueError(
                    f"verification requirement '{requirement_id}' is executable, not manual"
                )
            if not isinstance(approved, bool):
                raise ValueError("manual verification approval must be a boolean")
            if not isinstance(approver, str) or not approver.strip():
                raise ValueError("manual verification approver must be a non-blank string")
            if not isinstance(rationale, str) or not rationale.strip():
                raise ValueError("manual verification rationale must be a non-blank string")
            if not isinstance(evidence_hash, str) or not evidence_hash.strip():
                raise ValueError("manual verification evidence_hash must be non-blank")
            workspace_digest = workspace_fingerprint_digest(
                workspace_fingerprint(self.project_root, self.ledger.path)
            )
            return self.ledger.append(
                AuditEventType.HUMAN_DECISION,
                _event_payload(
                    {
                        "stage": f"verification:{requirement.id}",
                        "manual": True,
                        "approved": approved,
                        "approver": approver.strip(),
                        "rationale": rationale.strip(),
                        "evidence_hash": evidence_hash.strip(),
                        "workspace_fingerprint_sha256": workspace_digest,
                    },
                    metadata,
                ),
                contract_id=contract.goal_id,
            )

    def record_verifier(
        self,
        contract: GoalContract,
        decision: VerifierDecision,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> LedgerEvent:
        with self.execution_lock():
            self.frozen_contract(contract)
            return self.ledger.append(
                AuditEventType.VERIFIER_DECISION,
                _event_payload(decision.to_dict(), metadata),
                contract_id=contract.goal_id,
            )

    def evaluate_plan(self, contract: GoalContract) -> GateResult:
        return self.gate.evaluate_plan(contract)

    def can_execute_action(self, contract: GoalContract, action_kind: str) -> GateResult:
        return self.gate.can_execute_action(contract, action_kind)

    def complete(
        self,
        contract: GoalContract,
        *,
        min_passes: int = 2,
        event_metadata: Mapping[str, object] | None = None,
    ) -> GateResult:
        return self.gate.complete(
            contract,
            min_passes=min_passes,
            event_metadata=event_metadata,
        )

    def check_tool_allowed(self, contract: GoalContract, tool: str) -> GateResult:
        return self.gate.check_tool_allowed(contract, tool)

    def check_non_goal(self, contract: GoalContract, action_desc: str) -> GateResult:
        return self.gate.check_non_goal(contract, action_desc)

    def should_stop(
        self,
        contract: GoalContract,
        iteration_state: Mapping[str, int],
    ) -> GateResult:
        return self.gate.should_stop(contract, iteration_state)
