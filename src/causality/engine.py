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

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .agenda import Agenda, AgendaItem
from .agent_harness import AgentHarness, Dispatch, TaskType
from .contract_harness import ContractHarness
from .contracts import GateDecision, GoalContract, Risk, TaskContract
from .loop import LoopResult, StepOutcome, run_bounded_loop
from .memory import TypedMemory
from .orchestrator import Causality
from .reflect import Reflection, reflect_on_contract
from .review import ReviewResult, Verifier, run_review
from .skills import SkillCandidate, SkillStore

# A unit of work for one loop iteration: do the work (record evidence, etc.).
Work = Callable[[GoalContract, int], Any]


@dataclass(frozen=True)
class TaskRun:
    """The full result of one end-to-end task run."""

    dispatch: Dispatch
    task: TaskContract
    loop: LoopResult
    review: ReviewResult | None
    reflection: Reflection
    skill: SkillCandidate | None

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
        }


@dataclass
class CausalityEngine:
    """Wire the five layers into one runnable engine rooted at ``root``."""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.runtime = Causality(self.root / ".causality" / "ledger.jsonl")
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
        verification: Sequence[str],
        stop_condition: Mapping[str, Any],
        non_goals: Sequence[str] = (),
        allowed_tools: Sequence[str] = (),
        risk: Risk | str = Risk.LOW,
        summary: str = "",
        task_type: TaskType | str | None = None,
        min_passes: int = 2,
        distill_skill: bool = True,
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

        # L2 bind the immutable contract (the gateable GoalContract + frozen view).
        bound = self.harness.bind(
            objective=objective,
            summary=summary,
            verification=verification,
            stop_condition=stop_condition,
            non_goals=non_goals,
            allowed_tools=allowed_tools,
            risk=risk,
        )
        contract = bound.contract

        # L3 bounded loop: each iteration does the work then a standardized review
        # so the completion gate sees the recorded verifier passes.
        last_review: dict[str, ReviewResult] = {}

        def step(current: GoalContract, iteration: int) -> StepOutcome:
            work(current, iteration)
            review = run_review(self.runtime, current, verifiers, min_passes=min_passes)
            last_review["result"] = review
            return StepOutcome(progress=review.approved)

        loop_result = run_bounded_loop(self.runtime, contract, step, min_passes=min_passes)

        # L0 reflect: distill the contract's trail into typed memory.
        reflection = reflect_on_contract(self.runtime.ledger, self.memory, contract)

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
        )

    def run_next(
        self,
        *,
        work: Work,
        verifiers: Sequence[Verifier],
        verification: Sequence[str],
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
