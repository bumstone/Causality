"""Causality end-to-end runtime engine (ADR 0006 §2 / §6).

Ties the five layers into a single task run:

    Agenda (L0) -> Dispatch / Agent Harness (L1) -> Contract Harness (L2)
    -> bounded loop with automated Review (L3) -> Evidence Ledger (L4)
    -> Reflect distillation back into typed memory (L0) -> earned-skill candidate

This closes the front half of the self-improvement loop (Run -> Review -> Fix)
and seeds the back half (Reflect -> Skill update). The caller supplies a ``work``
callback (one unit of execution that records evidence) and a set of
``verifiers``; the engine wires dispatch, the frozen Task Contract, the bounded
loop, the standardized review, reflection, and skill distillation together.
"""

from __future__ import annotations

import inspect
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .agenda import Agenda, AgendaItem
from .agent_harness import AgentHarness, Dispatch, TaskType
from .playbooks import Playbook
from .contract_harness import ContractHarness
from .contracts import (
    AuditEventType,
    GateDecision,
    GoalContract,
    Risk,
    StateTransition,
    TaskContract,
    VerificationRequirement,
)
from .execution import ActionBlocked, ApprovePlan, ExecutionAdapter
from .gates import GateResult
from .loop import LoopResult, StepOutcome, run_bounded_loop
from .memory import MemoryEntry, TypedMemory
from .orchestrator import Causality
from .reflect import Reflection, reflect_on_contract
from .review import ReviewResult, Verifier, run_review
from .skills import SkillCandidate, SkillStore
from .verification import workspace_changes, workspace_fingerprint

# A unit of work for one loop iteration: do the work (record evidence, etc.).
# A three-arg ``work(contract, iteration, adapter)`` opts into per-action
# gating. The legacy two-arg form remains for one deprecation cycle and runs
# ungated because it cannot receive the ExecutionAdapter.
Work = Callable[..., Any]

# HITL hook for the guardrail read-path: given the active (non-expired) failures
# recalled for a run's failure_scope, return the non_goal clauses to inject. It
# returns curated clauses (not raw failure summaries) so a human phrases the
# boundary; returning nothing injects nothing, so a past failure becomes a
# standing guardrail only by explicit confirmation (ADR 0005 §2.5: guardrails
# must not auto-ratchet).
GuardrailConfirm = Callable[[Sequence[MemoryEntry]], Sequence[str]]


def _accepts_adapter(work: Work) -> bool:
    """Whether ``work`` takes the ExecutionAdapter as a third positional arg.

    Counts only positional parameters (``inspect.signature`` already drops
    ``self`` for bound methods); ``*args`` is treated as accepting it. Anything
    without an introspectable signature falls back to the two-arg call.
    """
    try:
        parameters = inspect.signature(work).parameters
    except (TypeError, ValueError):
        return False
    positional = 0
    for param in parameters.values():
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
    return positional >= 3


def _invoke_work(work: Work, contract: GoalContract, iteration: int, adapter: ExecutionAdapter) -> Any:
    """Call ``work``, handing it the gating adapter only if it accepts one."""
    if _accepts_adapter(work):
        return work(contract, iteration, adapter)
    warnings.warn(
        "two-argument work callbacks are deprecated; accept "
        "(contract, iteration, adapter) instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return work(contract, iteration)


@dataclass(frozen=True)
class TaskRun:
    """The full result of one end-to-end task run."""

    dispatch: Dispatch
    task: TaskContract
    loop: LoopResult
    review: ReviewResult | None
    reflection: Reflection
    skill: SkillCandidate | None
    recalled_skills: tuple[SkillCandidate, ...] = ()
    playbooks: tuple[Playbook, ...] = ()

    @property
    def passed(self) -> bool:
        return self.loop.decision is GateDecision.PASS

    def to_dict(self) -> dict[str, object]:
        return {
            "dispatch": self.dispatch.to_dict(),
            "task": self.task.to_dict(),
            "loop": {
                "decision": self.loop.decision.value,
                "iterations": self.loop.iterations,
                "reason": self.loop.reason,
            },
            "review": self.review.to_dict() if self.review is not None else None,
            "reflection": self.reflection.to_dict(),
            "skill": self.skill.to_dict() if self.skill is not None else None,
            "recalled_skills": [s.to_dict() for s in self.recalled_skills],
            "playbooks": [p.to_dict() for p in self.playbooks],
        }


@dataclass
class CausalityEngine:
    """Wire the five layers into one runnable engine rooted at ``root``."""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.runtime = Causality(
            self.root / ".causality" / "ledger.jsonl",
            project_root=self.root,
        )
        self.harness = ContractHarness(self.runtime)
        self.dispatcher = AgentHarness()
        self.memory = TypedMemory(self.root)
        self.skills = SkillStore(self.root)
        self.agenda = Agenda(self.root / ".causality" / "agenda.json")

    def run_task(
        self,
        *,
        objective: str,
        work: Work,
        verifiers: Sequence[Verifier],
        verification: Sequence[str | VerificationRequirement],
        stop_condition: Mapping[str, Any],
        non_goals: Sequence[str] = (),
        allowed_tools: Sequence[str] = (),
        risk: Risk | str = Risk.LOW,
        summary: str = "",
        task_type: TaskType | str | None = None,
        min_passes: int = 2,
        distill_skill: bool = True,
        approve_plan: ApprovePlan | None = None,
        failure_scope: str | None = None,
        confirm_guardrails: GuardrailConfirm | None = None,
        failure_ttl_days: int | None = None,
        authored_skills: Sequence[SkillCandidate] = (),
    ) -> TaskRun:
        """Run one task end to end and return its :class:`TaskRun`.

        Steps: classify+route -> bind a frozen Task Contract -> drive the bounded
        loop where each iteration runs ``work`` then an automated Review ->
        reflect into typed memory -> on success, distill an earned-skill
        candidate.
        """
        # L1 dispatch: an explicit TaskType wins; a string is first tried as a
        # TaskType value, then classified as free text; else classify the
        # objective. Without the value coercion, "long_running" keyword-misses
        # and falls to TRIVIAL (code review 2026-06-13, F7).
        if isinstance(task_type, TaskType):
            resolved_type = task_type
        elif isinstance(task_type, str):
            try:
                resolved_type = TaskType(task_type)
            except ValueError:
                resolved_type = self.dispatcher.classify(task_type)
        else:
            resolved_type = self.dispatcher.classify(objective)
        dispatch = self.dispatcher.route(resolved_type)
        # Resolve the bundle labels to vendored playbooks so the run carries its
        # structured phases (surfaced on the TaskRun) instead of bare label strings.
        playbooks = self.dispatcher.playbooks(dispatch)

        # Back-half read-path: recall promoted earned skills (and any authored
        # skills) relevant to this objective so they can be reused, authored
        # before earned. Surfaced on the TaskRun and handed to the ExecutionAdapter
        # so opted-in work can consult them.
        recalled_skills = tuple(self.skills.recall(objective, authored=authored_skills))

        # L0 -> L2 guardrail read-path: before freezing the contract, recall the
        # active (non-expired) failures recorded under this failure_scope and let
        # a human confirm which become non_goals, so past failures feed forward
        # as guardrails. active_only enforces the TTL loop (expired failures are
        # never offered) and the confirm hook prevents auto-ratcheting.
        guarded_non_goals = self._recall_guardrails(non_goals, failure_scope, confirm_guardrails)

        # L2 bind the immutable contract (the gateable GoalContract + frozen view).
        bound = self.harness.bind(
            objective=objective,
            summary=summary,
            verification=verification,
            stop_condition=stop_condition,
            non_goals=guarded_non_goals,
            allowed_tools=allowed_tools,
            risk=risk,
        )
        contract = bound.contract

        # L2 plan gate: a high-risk plan must clear human approval BEFORE any
        # execution. For an approval-required contract, consult the approve_plan
        # hook on the freshly bound contract -- a returned PlanApproval records
        # the plan-stage HUMAN_DECISION so evaluate_plan can pass (the caller had
        # no other way to approve a goal_id minted inside run_task). evaluate_plan
        # PASSes outright for a low-risk contract (the common case), so all of
        # this is a no-op there; an unapproved high-risk plan ESCALATEs and we
        # return without running ``work`` at all.
        if contract.approval_required and approve_plan is not None:
            approval = approve_plan(contract)
            if approval is not None:
                self.runtime.approve(contract, "plan", approval.approver, approval.rationale)
        plan_gate = self.runtime.evaluate_plan(contract)
        if not plan_gate.allowed:
            return self._gated_out(
                dispatch,
                bound.task,
                contract,
                plan_gate,
                failure_scope,
                failure_ttl_days,
                recalled_skills,
                playbooks,
            )

        # L3 bounded loop: each iteration does the work then a standardized review
        # so the completion gate sees the recorded verifier passes. The adapter
        # enforces the contract's per-action gates for any work that opts in; a
        # refused action raises ActionBlocked and terminates the loop below.
        adapter = ExecutionAdapter(self.runtime, contract, recalled_skills)
        last_review: dict[str, ReviewResult] = {}
        progress = {"iterations": 0}

        def step(current: GoalContract, iteration: int) -> StepOutcome:
            progress["iterations"] = iteration
            _invoke_work(work, current, iteration, adapter)
            for requirement in current.verification_requirements:
                if not requirement.required or requirement.manual:
                    continue
                result = self.runtime.verify_requirement(
                    current,
                    requirement.id,
                    root=self.root,
                )
                if result.status in {"blocked", "timeout", "error"}:
                    return StepOutcome(progress=False)
            before_review = workspace_fingerprint(self.root, self.runtime.ledger.path)
            previous_bytecode_policy = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            try:
                review = run_review(
                    self.runtime,
                    current,
                    verifiers,
                    min_passes=min_passes,
                    require_evidence=bool(current.verification_requirements),
                )
            finally:
                sys.dont_write_bytecode = previous_bytecode_policy
            last_review["result"] = review
            changed = workspace_changes(
                before_review,
                workspace_fingerprint(self.root, self.runtime.ledger.path),
            )
            if changed:
                self.runtime.ledger.append(
                    AuditEventType.TOOL_CALL,
                    {
                        "tool": "verifier",
                        "paths": changed[:100],
                        "mutates_task": True,
                    },
                    contract_id=current.goal_id,
                )
                self.runtime.transition(
                    current,
                    StateTransition.BLOCKED,
                    "verifier changed workspace state",
                )
                return StepOutcome(progress=False)
            return StepOutcome(progress=review.approved)

        try:
            loop_result = run_bounded_loop(self.runtime, contract, step, min_passes=min_passes)
        except ActionBlocked as blocked:
            # A per-action gate refused an action: the run terminates with the
            # gate's decision (STOP for a non-goal breach, ESCALATE for a tool or
            # irreversibility breach). The refused action never executed.
            loop_result = LoopResult(blocked.result.decision, progress["iterations"], str(blocked))

        # L0 reflect: distill the contract's trail into typed memory. Recording
        # failures under failure_scope lets the next run in that scope recall them.
        reflection = reflect_on_contract(
            self.runtime.ledger,
            self.memory,
            contract,
            failure_scope=failure_scope,
            failure_ttl_days=failure_ttl_days,
        )

        # Back half: on a clean pass, distill an earned-skill candidate.
        skill: SkillCandidate | None = None
        if distill_skill and loop_result.decision is GateDecision.PASS:
            skill = self.skills.distill(self.runtime.ledger, contract)

        return TaskRun(
            dispatch=dispatch,
            task=bound.task,
            loop=loop_result,
            review=last_review.get("result"),
            reflection=reflection,
            skill=skill,
            recalled_skills=recalled_skills,
            playbooks=playbooks,
        )

    def _recall_guardrails(
        self,
        non_goals: Sequence[str],
        failure_scope: str | None,
        confirm_guardrails: GuardrailConfirm | None,
    ) -> list[str]:
        """Recall active scoped failures and append the confirmed guardrails.

        Returns the caller's ``non_goals`` unchanged unless both a
        ``failure_scope`` and a ``confirm_guardrails`` hook are supplied: only
        then are the scope's active (non-expired) failures offered to the hook,
        whose returned clauses are appended. Duplicates are dropped so a repeated
        guardrail is not bound twice.
        """
        injected = list(non_goals)
        if failure_scope is None or confirm_guardrails is None:
            return injected
        candidates = [
            entry
            for entry in self.memory.entries("failures", active_only=True)
            if entry.metadata.get("scope") == failure_scope
        ]
        if candidates:
            injected.extend(clause for clause in confirm_guardrails(candidates) if clause and clause.strip())
        return list(dict.fromkeys(injected))

    def _gated_out(
        self,
        dispatch: Dispatch,
        task: TaskContract,
        contract: GoalContract,
        gate: GateResult,
        failure_scope: str | None = None,
        failure_ttl_days: int | None = None,
        recalled_skills: tuple[SkillCandidate, ...] = (),
        playbooks: tuple[Playbook, ...] = (),
    ) -> TaskRun:
        """Build the TaskRun for a plan refused at the plan gate.

        No ``work`` ran and there is no review, but Reflect still distills the
        contract's trail (the GOAL_CONTRACT plus the escalating GATE_DECISION) so
        the refusal lands in typed memory like any other terminal run.
        """
        reflection = reflect_on_contract(
            self.runtime.ledger,
            self.memory,
            contract,
            failure_scope=failure_scope,
            failure_ttl_days=failure_ttl_days,
        )
        reason = gate.reasons[0] if gate.reasons else "plan requires approval"
        return TaskRun(
            dispatch=dispatch,
            task=task,
            loop=LoopResult(gate.decision, 0, reason),
            review=None,
            reflection=reflection,
            skill=None,
            recalled_skills=recalled_skills,
            playbooks=playbooks,
        )

    def run_next(
        self,
        *,
        work: Work,
        verifiers: Sequence[Verifier],
        verification: Sequence[str | VerificationRequirement],
        stop_condition: Mapping[str, Any],
        **kwargs: Any,
    ) -> TaskRun | None:
        """Pull the next pending agenda item and run it end to end.

        Activates the item, runs the task, and marks it done on a clean pass.
        A run that fails, escalates, or raises defers the item back to pending
        so the intention is never stranded "active" forever (code review
        2026-06-13, F10). Returns ``None`` when the agenda has no pending work.
        """
        item: AgendaItem | None = self.agenda.next_pending()
        if item is None:
            return None
        self.agenda.activate(item.item_id)
        run: TaskRun | None = None
        try:
            run = self.run_task(
                objective=item.objective,
                work=work,
                verifiers=verifiers,
                verification=verification,
                stop_condition=stop_condition,
                **kwargs,
            )
            return run
        finally:
            if run is not None and run.passed:
                self.agenda.complete(item.item_id)
            else:
                self.agenda.defer(item.item_id)
