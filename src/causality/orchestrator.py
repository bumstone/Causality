from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts import (
    AuditEventType,
    EvidenceKind,
    GoalContract,
    StateTransition,
    VerifierDecision,
)
from .gates import GateResult, HITLGate
from .ledger import EvidenceLedger


class Causality:
    """Small orchestration facade for embedding in an Causality runtime."""

    def __init__(self, ledger_path: str | Path):
        self.ledger = EvidenceLedger(ledger_path)
        self.gate = HITLGate(self.ledger)

    def create_contract(self, contract: GoalContract) -> GoalContract:
        self.ledger.append(
            AuditEventType.GOAL_CONTRACT,
            contract.to_dict(),
            contract_id=contract.goal_id,
        )
        return contract

    def transition(
        self,
        contract: GoalContract,
        state: StateTransition | str,
        reason: str,
    ) -> None:
        state_value = state.value if isinstance(state, StateTransition) else str(state)
        contract.state = state_value
        self.ledger.append(
            AuditEventType.STATE_TRANSITION,
            {"state": state_value, "reason": reason},
            contract_id=contract.goal_id,
        )

    def approve(self, contract: GoalContract, stage: str, approver: str, rationale: str) -> None:
        self.ledger.append(
            AuditEventType.HUMAN_DECISION,
            {
                "stage": stage,
                "approved": True,
                "approver": approver,
                "rationale": rationale,
            },
            contract_id=contract.goal_id,
        )

    def reject(self, contract: GoalContract, stage: str, approver: str, rationale: str) -> None:
        self.ledger.append(
            AuditEventType.HUMAN_DECISION,
            {
                "stage": stage,
                "approved": False,
                "approver": approver,
                "rationale": rationale,
            },
            contract_id=contract.goal_id,
        )

    def record_evidence(
        self,
        contract: GoalContract,
        kind: EvidenceKind | str,
        payload: dict[str, Any],
        artifact_paths: Iterable[str | Path] = (),
    ) -> None:
        kind_value = kind.value if isinstance(kind, EvidenceKind) else str(kind)
        event_payload = {"kind": kind_value, **payload}
        self.ledger.append(
            AuditEventType.EVIDENCE,
            event_payload,
            contract_id=contract.goal_id,
            artifact_paths=artifact_paths,
        )

    def record_verifier(self, contract: GoalContract, decision: VerifierDecision) -> None:
        self.ledger.append(
            AuditEventType.VERIFIER_DECISION,
            decision.to_dict(),
            contract_id=contract.goal_id,
        )

    def evaluate_plan(self, contract: GoalContract) -> GateResult:
        return self.gate.evaluate_plan(contract)

    def can_execute_action(self, contract: GoalContract, action_kind: str) -> GateResult:
        return self.gate.can_execute_action(contract, action_kind)

    def complete(self, contract: GoalContract, *, min_passes: int = 2) -> GateResult:
        return self.gate.complete(contract, min_passes=min_passes)

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
