from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .contracts import (
    AuditEventType,
    GateDecision,
    GoalContract,
    IRREVERSIBLE_ACTIONS,
    Risk,
    VerifierDecision,
)
from .ledger import EvidenceLedger, LedgerEvent


@dataclass(frozen=True)
class GateResult:
    decision: GateDecision
    reasons: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision == GateDecision.PASS

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "reasons": list(self.reasons),
            "allowed": self.allowed,
        }


class HITLGate:
    """Policy gate for plan approval, action execution, and completion."""

    def __init__(self, ledger: EvidenceLedger):
        self.ledger = ledger

    def evaluate_plan(self, contract: GoalContract) -> GateResult:
        if contract.approval_required and not self._has_human_approval(contract.goal_id, "plan"):
            return self._record(
                contract,
                GateDecision.ESCALATE,
                "high-risk plan requires human approval",
            )
        return self._record(contract, GateDecision.PASS, "plan may proceed")

    def can_execute_action(self, contract: GoalContract, action_kind: str) -> GateResult:
        action_requires_approval = (
            contract.risk_value == Risk.IRREVERSIBLE.value
            or action_kind in IRREVERSIBLE_ACTIONS
        )
        if action_requires_approval and not self._has_human_approval(contract.goal_id, action_kind):
            return self._record(
                contract,
                GateDecision.ESCALATE,
                f"action '{action_kind}' requires human approval",
            )
        return self._record(contract, GateDecision.PASS, f"action '{action_kind}' may execute")

    def complete(
        self,
        contract: GoalContract,
        verifier_decisions: Iterable[VerifierDecision] | None = None,
    ) -> GateResult:
        verifier_records = list(verifier_decisions or self._verifier_decisions(contract.goal_id))
        if any(item.is_critical_failure for item in verifier_records):
            return self._record(
                contract,
                GateDecision.REPAIR,
                "critical verifier failure remains unresolved",
            )

        pass_count = sum(1 for item in verifier_records if item.is_pass)
        if pass_count < 2:
            return self._record(
                contract,
                GateDecision.REPAIR,
                "completion requires at least two independent verifier passes",
            )

        missing = self._missing_required_evidence(contract)
        if missing:
            return self._record(
                contract,
                GateDecision.REPAIR,
                "missing required evidence: " + ", ".join(sorted(missing)),
            )

        if contract.approval_required and not self._has_human_approval(contract.goal_id, "final"):
            return self._record(
                contract,
                GateDecision.ESCALATE,
                "final human approval required for high-risk contract",
            )

        return self._record(contract, GateDecision.PASS, "completion criteria satisfied")

    def _missing_required_evidence(self, contract: GoalContract) -> set[str]:
        required = contract.required_evidence_kinds()
        if not required:
            return set()
        seen = {
            event.payload.get("kind")
            for event in self.ledger.find(AuditEventType.EVIDENCE)
            if event.contract_id == contract.goal_id
        }
        return required - seen

    def _has_human_approval(self, contract_id: str, stage: str) -> bool:
        for event in self.ledger.find(AuditEventType.HUMAN_DECISION):
            if event.contract_id != contract_id:
                continue
            if event.payload.get("stage") == stage and event.payload.get("approved") is True:
                return True
        return False

    def _verifier_decisions(self, contract_id: str) -> list[VerifierDecision]:
        decisions: list[VerifierDecision] = []
        for event in self.ledger.find(AuditEventType.VERIFIER_DECISION):
            if event.contract_id != contract_id:
                continue
            payload = event.payload
            decisions.append(
                VerifierDecision(
                    verifier=payload.get("verifier", "unknown"),
                    status=payload.get("status", "fail"),
                    rationale=payload.get("rationale", ""),
                    severity=payload.get("severity", "normal"),
                    evidence_refs=tuple(payload.get("evidence_refs", ())),
                    created_at=payload.get("created_at", event.timestamp),
                )
            )
        return decisions

    def _record(self, contract: GoalContract, decision: GateDecision, reason: str) -> GateResult:
        result = GateResult(decision=decision, reasons=(reason,))
        self.ledger.append(
            AuditEventType.GATE_DECISION,
            result.to_dict(),
            contract_id=contract.goal_id,
        )
        return result
