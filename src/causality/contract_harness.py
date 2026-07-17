"""Contract Harness (ADR 0003).

A mandatory pre-run ritual: before any execution, fix *what to finish* and
*what not to do*, then freeze a :class:`TaskContract`. The harness produces and
records the contract exactly once (a single ``GOAL_CONTRACT`` ledger event), so
it does not add a loop stage or widen the goal scope (ADR 0001).
"""

from __future__ import annotations

import os
import shlex
import warnings
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .contracts import (
    EvidenceKind,
    GoalContract,
    PermissionContract,
    Risk,
    TaskContract,
    VerificationRequirement,
)
from .orchestrator import Causality


# The ceilings should_stop actually consumes (gates.py); bind() requires at
# least one of them to be a positive int so every bound loop is truly bounded.
STOP_CONDITION_KEYS = ("max_iterations", "no_progress_iterations", "max_failed_hypotheses")


def _split_legacy_command(command: str) -> tuple[str, ...]:
    """Split the deprecated string form without ever invoking a shell."""
    if os.name != "nt":
        return tuple(shlex.split(command, posix=True))

    import ctypes

    argc = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = command_line_to_argv(command, ctypes.byref(argc))
    if not argv:
        raise ValueError("invalid Windows command line")
    try:
        return tuple(argv[index] for index in range(argc.value))
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


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
        verification: Sequence[str | VerificationRequirement],
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

        requirements: list[VerificationRequirement] = []
        legacy_commands: list[str] = []
        reserved_ids = {
            item.id for item in verification if isinstance(item, VerificationRequirement)
        }
        for item in verification:
            if isinstance(item, VerificationRequirement):
                requirements.append(item)
                continue
            if not isinstance(item, str):
                raise ContractHarnessError(
                    "verification entries must be VerificationRequirement or legacy strings"
                )
            command = item.strip()
            if not command:
                continue
            argv = _split_legacy_command(command)
            if not argv:
                continue
            legacy_commands.append(command)
            sequence = len(legacy_commands)
            requirement_id = f"verify-{sequence:03d}"
            while requirement_id in reserved_ids:
                sequence += 1
                requirement_id = f"verify-{sequence:03d}"
            reserved_ids.add(requirement_id)
            requirements.append(
                VerificationRequirement(
                    id=requirement_id,
                    argv=argv,
                )
            )
        if not requirements:
            raise ContractHarnessError(
                "at least one verification command/criterion is required (step 4)"
            )
        if legacy_commands:
            warnings.warn(
                "string verification commands are deprecated; pass VerificationRequirement "
                "objects instead",
                DeprecationWarning,
                stacklevel=2,
            )
        if evidence_kind != EvidenceKind.TEST_OUTPUT:
            warnings.warn(
                "evidence_kind is ignored for executable verification and will be removed",
                DeprecationWarning,
                stacklevel=2,
            )

        if not stop_condition:
            raise ContractHarnessError("stop_condition is required (step 5: when to stop)")
        # A stop condition must GUARANTEE termination. `no_progress_iterations`
        # and `max_failed_hypotheses` depend on the step's self-reported
        # progress/failure, which can be wrong or flap forever (codex review
        # r3407165600); only `max_iterations` is an unconditional ceiling. So
        # require it as the backstop -- the others are optional refinements. A
        # typo'd or zero value ({"foo": 1}, {"max_iterations": 0}) is rejected.
        max_iterations = stop_condition.get("max_iterations")
        if not (
            isinstance(max_iterations, int)
            and not isinstance(max_iterations, bool)
            and max_iterations > 0
        ):
            raise ContractHarnessError(
                "stop_condition must set a positive integer 'max_iterations' as the "
                "unconditional termination backstop (additional ceilings allowed: "
                + ", ".join(k for k in STOP_CONDITION_KEYS if k != "max_iterations")
                + ")"
            )

        # ``evidence_kind`` remains in the one-minor compatibility signature,
        # but executable requirements are always recorded as verification_result
        # evidence. Generic EvidenceRequirement remains available on GoalContract
        # for non-executable evidence contracts created directly by callers.
        _ = evidence_kind
        contract = GoalContract(
            title=objective,
            summary=summary,
            risk=risk,
            permissions=PermissionContract(allowed_tools=tuple(allowed_tools)),
            verification_requirements=tuple(requirements),
            non_goals=tuple(n for n in non_goals if n and n.strip()),
            stopping_policy=dict(stop_condition),
        )
        self.runtime.create_contract(contract)
        return BoundContract(contract=contract, task=TaskContract.of(contract))
