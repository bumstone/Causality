from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

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

    def check_tool_allowed(self, contract: GoalContract, tool: str) -> GateResult:
        """Enforce the Allowed-tools clause (ADR 0001 §2.3).

        An empty ``allowed_tools`` means no restriction was declared, so any
        tool passes. When a restriction is declared, a tool outside it
        escalates so a human can decide whether to widen scope.
        """
        allowed = contract.permissions.allowed_tools
        if allowed and tool not in allowed:
            return self._record(
                contract,
                GateDecision.ESCALATE,
                f"tool '{tool}' is outside the contract's allowed_tools",
            )
        return self._record(contract, GateDecision.PASS, f"tool '{tool}' is permitted")

    def check_non_goal(self, contract: GoalContract, action_desc: str) -> GateResult:
        """Enforce the Non-goals clause (ADR 0001 §2.3).

        A non-goal is a hard boundary, so a match stops the action rather than
        escalating: the contract has already declared this work out of scope.
        """
        text = action_desc.lower()
        for non_goal in contract.non_goals:
            needle = non_goal.strip().lower()
            if needle and needle in text:
                return self._record(
                    contract,
                    GateDecision.STOP,
                    f"action conflicts with declared non-goal: {non_goal}",
                )
        return self._record(contract, GateDecision.PASS, "action does not hit a non-goal")

    def should_stop(
        self,
        contract: GoalContract,
        iteration_state: Mapping[str, int],
    ) -> GateResult:
        """Enforce the Stop-condition clause by reading ``stopping_policy``.

        This is the consumer the policy previously lacked (ADR 0006 C-STOP-1).
        ``iteration_state`` is supplied by the loop runtime. Hitting the
        iteration or no-progress ceiling stops; exhausting failed hypotheses
        escalates (consistent with the root-cause protocol).
        """
        policy = contract.stopping_policy
        iterations = int(iteration_state.get("iterations", 0))
        no_progress = int(iteration_state.get("no_progress_iterations", 0))
        failed = int(iteration_state.get("failed_hypotheses", 0))

        max_iterations = int(policy.get("max_iterations", 0) or 0)
        max_no_progress = int(policy.get("no_progress_iterations", 0) or 0)
        max_failed = int(policy.get("max_failed_hypotheses", 0) or 0)

        if max_iterations and iterations >= max_iterations:
            return self._record(
                contract, GateDecision.STOP, f"reached max_iterations ({max_iterations})"
            )
        if max_no_progress and no_progress >= max_no_progress:
            return self._record(
                contract,
                GateDecision.STOP,
                f"no progress for {max_no_progress} iteration(s)",
            )
        if max_failed and failed >= max_failed:
            return self._record(
                contract,
                GateDecision.ESCALATE,
                f"reached max_failed_hypotheses ({max_failed})",
            )
        return self._record(contract, GateDecision.PASS, "stop condition not met")

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
