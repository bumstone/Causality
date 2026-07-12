"""Vendored playbooks for the L1 dispatch bundles (ADR 0004).

``agent_harness._ROUTING`` maps each task type to a tuple of *bundle labels*
(e.g. ``("tdd", "debugging")``). Those labels were just strings -- nothing
resolved them to anything runnable. This module vendors each label as a
structured :class:`Playbook` (ordered phases with concrete steps), and
:func:`resolve_playbooks` turns a dispatch's labels into those playbooks, so a
run can surface and follow them (``AgentHarness.playbooks`` /
``CausalityEngine.run_task`` attach the resolved playbooks to the ``TaskRun``).

The phases are the contract of *how* a bundle is run; the agent executes them,
the harness just guarantees every routed label resolves to a real, recorded
playbook instead of a dangling string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


class UnknownPlaybookError(KeyError):
    """Raised when a routed bundle label has no vendored playbook."""


@dataclass(frozen=True)
class PlaybookPhase:
    name: str
    steps: tuple[str, ...]
    requires_action: bool = False
    requires_verification: bool = False
    requires_verdicts: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "steps": list(self.steps)}


@dataclass(frozen=True)
class Playbook:
    name: str
    summary: str
    phases: tuple[PlaybookPhase, ...]

    @property
    def phase_names(self) -> tuple[str, ...]:
        return tuple(phase.name for phase in self.phases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "phases": [phase.to_dict() for phase in self.phases],
        }


def _phase(
    name: str,
    steps: tuple[str, ...],
    *,
    action: bool = False,
    verification: bool = False,
    verdicts: int = 2,
) -> PlaybookPhase:
    return PlaybookPhase(name, steps, action, verification, verdicts)


def _playbook(
    name: str,
    summary: str,
    *phases: PlaybookPhase | tuple[str, tuple[str, ...]],
) -> Playbook:
    materialized = tuple(
        phase if isinstance(phase, PlaybookPhase) else PlaybookPhase(*phase)
        for phase in phases
    )
    names = [phase.name for phase in materialized]
    if len(names) != len(set(names)):
        raise ValueError(f"playbook {name!r} contains duplicate phase names")
    return Playbook(
        name=name,
        summary=summary,
        phases=materialized,
    )


# One vendored playbook per bundle label used in agent_harness._ROUTING.
PLAYBOOKS: dict[str, Playbook] = {
    "office-hours": _playbook(
        "office-hours",
        "Frame a plan before building.",
        ("frame", ("State the goal and constraints", "List unknowns and assumptions")),
        ("options", ("Enumerate candidate approaches", "Note trade-offs and risks")),
        ("decision", ("Pick an approach with rationale", "Define acceptance criteria")),
    ),
    "ceo-review": _playbook(
        "ceo-review",
        "Review a plan for scope and risk before execution.",
        ("scope-check", ("Confirm the plan matches the goal", "Flag scope creep / non-goals")),
        ("risk-review", ("Identify high-risk/irreversible steps", "Require approval where needed")),
        ("sign-off", ("Approve, or send back with required changes",)),
    ),
    "tdd": _playbook(
        "tdd",
        "Red/green/refactor for code (acceptance-check-first otherwise).",
        _phase("red", ("Write a failing test for the expected behavior",), action=True),
        _phase("green", ("Implement the minimum to pass the test",), action=True),
        _phase(
            "refactor",
            ("Clean up while keeping the suite green",),
            action=True,
            verification=True,
        ),
    ),
    "debugging": _playbook(
        "debugging",
        "Prove the root cause before fixing.",
        _phase("reproduce", ("Reproduce the failure deterministically",), action=True),
        _phase("isolate", ("Narrow to the root cause", "Disprove hypotheses with evidence")),
        _phase("fix", ("Apply the fix at the root cause, not the symptom",), action=True),
        _phase(
            "verify",
            ("Add a regression test", "Confirm the suite is green"),
            verification=True,
        ),
    ),
    "root-cause-protocol": _playbook(
        "root-cause-protocol",
        "Reproduce, test one hypothesis at a time, verify it, then fix.",
        _phase("reproduce", ("Reproduce the failure deterministically",), action=True),
        _phase("hypothesis", ("Record and test one root-cause hypothesis",)),
        _phase("verify", ("Verify the supported root cause",), verification=True),
        _phase("fix", ("Fix the verified root cause",), action=True),
    ),
    "contract-harness": _playbook(
        "contract-harness",
        "Bind and gate the work before any execution.",
        ("bind", ("Freeze the goal contract: non-goals, tools, verification",)),
        ("gate", ("Approve high-risk plans before execution",)),
    ),
    "limited-causality-loop": _playbook(
        "limited-causality-loop",
        "Run a bounded work/review loop.",
        ("step", ("Do one bounded unit of work, recording evidence",)),
        ("review", ("Run independent verifiers against the contract",)),
        ("stop-check", ("Honor max_iterations / no-progress / failed-hypothesis limits",)),
    ),
    "ship": _playbook(
        "ship",
        "Release through the approved path.",
        ("pre-flight", ("Confirm tests green and acceptance criteria met",)),
        ("release", ("Cut the release through the approved path",)),
        ("post-flight", ("Verify the release and record evidence",)),
    ),
    "qa-checklist": _playbook(
        "qa-checklist",
        "Gate release on a verified checklist.",
        ("checks", ("Run the QA checklist", "Capture raw evidence for each item")),
        ("sign-off", ("Block release on any failed required check",)),
    ),
}


def resolve_playbooks(labels: Iterable[str]) -> tuple[Playbook, ...]:
    """Resolve dispatch bundle labels to vendored :class:`Playbook` objects.

    Raises :class:`UnknownPlaybookError` for any label without a vendored
    playbook, so a routing table can never point at a non-existent playbook.
    """
    resolved: list[Playbook] = []
    for label in labels:
        try:
            resolved.append(PLAYBOOKS[label])
        except KeyError as exc:
            raise UnknownPlaybookError(
                f"no vendored playbook for bundle label {label!r}"
            ) from exc
    return tuple(resolved)


def build_phase_plan(playbooks: Iterable[Playbook]) -> tuple[dict[str, Any], ...]:
    """Flatten selected playbooks into one ordered, immutable runner snapshot."""

    plan: list[dict[str, Any]] = []
    seen: set[str] = set()
    for playbook in playbooks:
        for phase in playbook.phases:
            phase_id = f"{playbook.name}/{phase.name}"
            if phase_id in seen:
                raise ValueError(f"duplicate workflow phase id: {phase_id}")
            seen.add(phase_id)
            plan.append(
                {
                    "phase_id": phase_id,
                    "playbook": playbook.name,
                    "name": phase.name,
                    "steps": list(phase.steps),
                    "requires_action": phase.requires_action,
                    "requires_verification": phase.requires_verification,
                    "requires_verdicts": phase.requires_verdicts,
                }
            )
    return tuple(plan)
