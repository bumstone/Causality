"""Contract Harness (ADR 0003).

A mandatory pre-run ritual: before any execution, fix *what to finish* and
*what not to do*, then freeze a :class:`TaskContract`. The harness produces and
records the contract exactly once (a single ``GOAL_CONTRACT`` ledger event), so
it does not add a loop stage or widen the goal scope (ADR 0001).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .contracts import (
    EvidenceKind,
    EvidenceRequirement,
    GoalContract,
    PermissionContract,
    Risk,
    TaskContract,
)
from .orchestrator import Causality


class ContractHarnessError(ValueError):
    """Raised when the pre-run ritual is incomplete.

    The ritual is the gate between "what we might do" and "what we are bound to
    do"; an incomplete contract must not reach execution.
    """


@dataclass(frozen=True)
class BoundContract:
    """The result of the pre-run ritual.

    Carries both the gateable ``GoalContract`` (pass this to the runtime gates:
    ``evaluate_plan``, ``can_execute_action``, ``check_tool_allowed``,
    ``check_non_goal``, ``should_stop``, ``complete``) and the immutable
    ``TaskContract`` view of the binding clauses. Returning only the latter
    would strand callers with no object to feed the enforcement path.
    """

    contract: GoalContract
    task: TaskContract


@dataclass(frozen=True)
class ContractHarness:
    """Bind a task to an immutable contract before execution.

    The five steps map onto :class:`TaskContract` clauses:

    1. objective       -> objective
    2. non_goals       -> non_goals
    3. allowed_tools   -> allowed_tools
    4. verification    -> verification (required evidence)
    5. stop_condition  -> stop_condition
    """

    runtime: Causality

    def bind(
        self,
        *,
        objective: str,
        verification: Sequence[str],
        stop_condition: Mapping[str, Any],
        non_goals: Sequence[str] = (),
        allowed_tools: Sequence[str] = (),
        risk: Risk | str = Risk.LOW,
        summary: str = "",
        evidence_kind: EvidenceKind | str = EvidenceKind.TEST_OUTPUT,
    ) -> BoundContract:
        objective = objective.strip()
        if not objective:
            raise ContractHarnessError("objective is required (step 1: summarize objective)")

        verification = tuple(v.strip() for v in verification if v and v.strip())
        if not verification:
            raise ContractHarnessError(
                "at least one verification command/criterion is required (step 4)"
            )

        if not stop_condition:
            raise ContractHarnessError("stop_condition is required (step 5: when to stop)")

        evidence_required = [
            EvidenceRequirement(kind=evidence_kind, description=command, required=True)
            for command in verification
        ]
        contract = GoalContract(
            title=objective,
            summary=summary,
            risk=risk,
            permissions=PermissionContract(allowed_tools=tuple(allowed_tools)),
            evidence_required=evidence_required,
            non_goals=tuple(n for n in non_goals if n and n.strip()),
            stopping_policy=dict(stop_condition),
        )
        self.runtime.create_contract(contract)
        return BoundContract(contract=contract, task=TaskContract.of(contract))
