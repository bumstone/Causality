"""Per-action execution gate (ADR 0001 §2.3, ADR 0003).

The Contract Harness freezes *what not to do* (``non_goals``), *which tools are
in scope* (``allowed_tools``), and the risk class -- but binding those clauses is
inert unless something enforces them at the moment an action runs. The engine
previously drove ``work -> review`` and never asked ``check_non_goal`` /
``check_tool_allowed`` / ``can_execute_action`` (code review 2026-06-13, P0).

:class:`ExecutionAdapter` is that enforcement point. A task's ``work`` routes
every side-effecting action through :meth:`ExecutionAdapter.execute`, which runs
the contract's per-action gates before the action touches the world. A refused
gate raises :class:`ActionBlocked` so the action never executes and the bounded
loop terminates with the gate's decision instead of silently proceeding.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Sequence, TypeVar

from .contracts import GoalContract
from .gates import GateResult
from .skills import SkillCandidate

T = TypeVar("T")


@dataclass(frozen=True)
class PlanApproval:
    """A human approval of a freshly bound high-risk plan (HITL).

    ``run_task`` binds a fresh contract (a new ``goal_id``) internally, so a
    caller cannot pre-record the plan-stage ``HUMAN_DECISION`` an
    approval-required contract needs. An ``approve_plan`` hook closes that gap:
    it receives the bound contract and returns a :class:`PlanApproval` to record
    that decision (so ``evaluate_plan`` can pass), or ``None`` to decline and let
    the plan ESCALATE.
    """

    approver: str
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.approver, str) or not self.approver.strip():
            raise ValueError("approval approver must be a non-blank string")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError("approval rationale must be a non-blank string")


# Hook consulted for an approval-required plan: bound contract -> approval|None.
ApprovePlan = Callable[[GoalContract], Optional[PlanApproval]]


class _Gated(Protocol):
    """The slice of the runtime an adapter enforces (orchestrator.Causality).

    Declared structurally so the adapter does not import the orchestrator (which
    imports the gates), avoiding an import cycle while staying type-checked.
    """

    def frozen_contract(self, contract: GoalContract) -> GoalContract: ...
    def execution_lock(self) -> AbstractContextManager[None]: ...
    def check_non_goal(self, contract: GoalContract, action_desc: str) -> GateResult: ...
    def check_tool_allowed(self, contract: GoalContract, tool: str) -> GateResult: ...
    def check_network_scope(self, contract: GoalContract, origin: str) -> GateResult: ...
    def check_auth_scope(self, contract: GoalContract, auth_ref: str | None) -> GateResult: ...
    def can_execute_action(self, contract: GoalContract, action_kind: str) -> GateResult: ...


class ActionBlocked(Exception):
    """Raised when a per-action gate refuses an action mid-work.

    Carries the refusing :class:`GateResult` -- ``STOP`` for a non-goal breach,
    ``ESCALATE`` for an out-of-scope tool or an unapproved irreversible action --
    so the engine can terminate the run with that exact decision.
    """

    def __init__(
        self,
        result: GateResult,
        *,
        tool: str,
        action_kind: str,
        description: str,
    ) -> None:
        self.result = result
        self.tool = tool
        self.action_kind = action_kind
        self.description = description
        reason = result.reasons[0] if result.reasons else "action blocked by gate"
        super().__init__(f"action blocked ({result.decision.value}): {reason}")


@dataclass(frozen=True)
class ExecutionAdapter:
    """Gate-enforcing executor handed to a task's ``work`` callback.

    Besides gating actions, it carries ``recalled_skills`` -- the authored/earned
    skills the engine recalled for this objective (authored before earned) -- so
    opted-in work can reuse a proven procedure instead of rediscovering it.
    """

    runtime: _Gated
    contract: GoalContract
    recalled_skills: tuple[SkillCandidate, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "contract", self.runtime.frozen_contract(self.contract))

    def execute(
        self,
        *,
        tool: str,
        action_kind: str,
        description: str,
        run: Callable[[], T],
        network_origin: str | None = None,
        network_origins: Sequence[str] = (),
        auth_ref: str | None = None,
    ) -> T:
        """Run ``run`` only if the contract's per-action gates all pass.

        Gates are checked in order and short-circuit on the first refusal, so a
        blocked action records only the gates up to (and including) the one that
        stopped it -- not a spurious PASS for the gates after it. The order is
        deliberate: ``check_non_goal`` is a hard boundary (STOP), so a breach
        should not even reach the tool/irreversibility checks. A refusal raises
        :class:`ActionBlocked`; otherwise ``run`` executes and its result is
        returned.
        """
        with self.runtime.execution_lock():
            checks = [
                lambda: self.runtime.check_non_goal(self.contract, description),
                lambda: self.runtime.check_tool_allowed(self.contract, tool),
            ]
            origins = tuple(
                dict.fromkeys(
                    (*network_origins,)
                    if network_origin is None
                    else (network_origin, *network_origins)
                )
            )
            for origin in origins:
                checks.append(
                    lambda origin=origin: self.runtime.check_network_scope(
                        self.contract,
                        origin,
                    )
                )
            if origins or auth_ref is not None:
                checks.append(lambda: self.runtime.check_auth_scope(self.contract, auth_ref))
            checks.append(lambda: self.runtime.can_execute_action(self.contract, action_kind))
            for check in checks:
                result = check()
                if not result.allowed:
                    raise ActionBlocked(
                        result, tool=tool, action_kind=action_kind, description=description
                    )
            return run()
