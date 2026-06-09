"""Bounded Causality loop driver (ADR 0006 §6.1, step 1).

The orchestrator facade exposes primitives but does not run a loop. This driver
binds the Run -> Review -> Fix cycle to the contract's stop condition via
``HITLGate.should_stop`` -- the consumer ``stopping_policy`` previously lacked --
so a "limited Causality loop" is bounded by ``max_iterations`` /
``no_progress_iterations`` / ``max_failed_hypotheses`` instead of running
unbounded.

The caller supplies a ``step`` callback that performs one unit of work (record
evidence, run a check, etc.) and reports whether it made progress. The driver
checks ``should_stop`` before each step and ``complete`` after each step.

Note: an empty ``stopping_policy`` configures no ceiling and would loop forever;
the Contract Harness (ADR 0003) requires a stop condition for this reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Union

from .contracts import GateDecision, GoalContract
from .orchestrator import Causality


@dataclass(frozen=True)
class StepOutcome:
    """What one loop step reports back to the driver."""

    progress: bool = True
    failed_hypothesis: bool = False


@dataclass(frozen=True)
class LoopResult:
    decision: GateDecision
    iterations: int
    reason: str


Step = Callable[[GoalContract, int], Union[StepOutcome, bool, None]]


def _normalize(value: Union[StepOutcome, bool, None]) -> StepOutcome:
    if value is None:
        return StepOutcome()
    if isinstance(value, StepOutcome):
        return value
    if isinstance(value, bool):
        return StepOutcome(progress=value)
    return StepOutcome()


def run_bounded_loop(
    runtime: Causality,
    contract: GoalContract,
    step: Step,
) -> LoopResult:
    """Drive ``step`` until completion passes or the stop condition fires.

    Returns the terminal :class:`LoopResult`:

    - ``PASS``: the completion gate passed.
    - ``STOP``: a stop-condition ceiling (iterations / no-progress) was hit.
    - ``ESCALATE``: completion needs human approval, or failed hypotheses were
      exhausted.
    """
    iterations = 0
    no_progress = 0
    failed = 0

    def _reason(result, default: str) -> str:
        return result.reasons[0] if result.reasons else default

    while True:
        state = {
            "iterations": iterations,
            "no_progress_iterations": no_progress,
            "failed_hypotheses": failed,
        }
        stop = runtime.should_stop(contract, state)
        if stop.decision is not GateDecision.PASS:
            return LoopResult(stop.decision, iterations, _reason(stop, "stop condition met"))

        outcome = _normalize(step(contract, iterations))
        iterations += 1
        if outcome.failed_hypothesis:
            failed += 1
        no_progress = 0 if outcome.progress else no_progress + 1

        result = runtime.complete(contract)
        if result.decision is GateDecision.PASS:
            return LoopResult(GateDecision.PASS, iterations, _reason(result, "completed"))
        if result.decision is GateDecision.ESCALATE:
            return LoopResult(GateDecision.ESCALATE, iterations, _reason(result, "escalate"))
        # REPAIR: loop again (replan), bounded by should_stop on the next round.
