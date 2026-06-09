from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# The three execution-control layers (ADR 0002). Each workflow is tagged with
# its primary layer; some straddle layers (e.g. writing-plans and TDD), in which
# case the tag is the layer that owns the workflow's gate.
CONTROL_LAYERS = ("stage_designer", "planner", "executor")


@dataclass(frozen=True)
class WorkflowTemplate:
    name: str
    purpose: str
    required_inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    gate: str
    layer: str = ""
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "required_inputs": list(self.required_inputs),
            "outputs": list(self.outputs),
            "gate": self.gate,
            "layer": self.layer,
            "notes": list(self.notes),
        }


CAUSALITY_WORKFLOWS: dict[str, WorkflowTemplate] = {
    "writing-plans": WorkflowTemplate(
        name="writing-plans",
        purpose="Create path-specific plans with acceptance criteria and verification commands.",
        required_inputs=("goal_contract", "repo_context", "constraints"),
        outputs=("immutable_plan_snapshot", "acceptance_criteria", "verification_commands"),
        gate="goal_scope_or_high_risk_plan_approval",
        layer="stage_designer",
        notes=("No placeholders", "Attach plan snapshot to the ledger before execution"),
    ),
    "subagent-driven-development": WorkflowTemplate(
        name="subagent-driven-development",
        purpose="Assign fresh bounded task packets to subagents while controller retains orchestration.",
        required_inputs=("seed_id", "task_id", "allowed_tools", "context_packet"),
        outputs=("subagent_report", "evidence_refs", "uncertainties"),
        gate="subagent_output_verifier_review",
        layer="stage_designer",
        notes=("Do not share full session context", "Use disjoint write scopes for parallel workers"),
    ),
    "verification-before-completion": WorkflowTemplate(
        name="verification-before-completion",
        purpose="Block completion until fresh evidence proves the acceptance criteria.",
        required_inputs=("acceptance_criteria", "evidence_requirements", "ledger_tail"),
        outputs=("verification_report", "missing_evidence", "verifier_decisions"),
        gate="completion_gate",
        layer="executor",
        notes=("Agent prose is a claim, not evidence", "Use raw tool output or artifact hashes"),
    ),
    "test-driven-development": WorkflowTemplate(
        name="test-driven-development",
        purpose="Use RED/GREEN/REFACTOR for code and acceptance-check-first for non-code work.",
        required_inputs=("expected_behavior", "test_surface", "implementation_scope"),
        outputs=("failing_check", "passing_check", "regression_artifact"),
        gate="verification_gate",
        layer="planner",
        notes=("Do not skip the failing check when a regression can be expressed",),
    ),
    "root-cause-protocol": WorkflowTemplate(
        name="root-cause-protocol",
        purpose="Investigate and prove root cause before applying a fix.",
        required_inputs=("symptom", "reproduction_steps", "affected_scope"),
        outputs=("root_cause_hypothesis", "confirming_evidence", "fix_plan"),
        gate="three_failed_hypotheses_escalation",
        layer="planner",
        notes=("After three failed hypotheses, escalate to HITL", "Avoid symptom-only fixes"),
    ),
    "session-bootstrap": WorkflowTemplate(
        name="session-bootstrap",
        purpose="Load only active seed, ledger tail, relevant memory, and current permissions.",
        required_inputs=("active_seed", "ledger_tail", "memory_facts", "permissions"),
        outputs=("context_packet", "open_questions", "allowed_next_actions"),
        gate="context_sufficiency_check",
        layer="stage_designer",
        notes=("Do not inject entire skill libraries every turn", "Only verified facts enter memory"),
    ),
}


def workflow_manifest() -> dict[str, Any]:
    return {
        "package": "causality-workflows",
        "version": "0.1.0",
        "workflows": [template.to_dict() for template in CAUSALITY_WORKFLOWS.values()],
    }


def build_subagent_packet(
    *,
    seed_id: str,
    task_id: str,
    allowed_tools: list[str],
    context: dict[str, Any],
    evidence_format: str = "ledger_event_refs",
) -> dict[str, Any]:
    return {
        "seed_id": seed_id,
        "task_id": task_id,
        "allowed_tools": allowed_tools,
        "context": context,
        "expected_output": {
            "summary": "what changed or what was discovered",
            "evidence_format": evidence_format,
            "uncertainties": "anything the controller must decide",
        },
    }


def build_session_bootstrap(
    *,
    active_seed: dict[str, Any],
    ledger_tail: list[dict[str, Any]],
    memory_facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    memory_facts = memory_facts or []
    return {
        "active_seed": active_seed,
        "ledger_tail": ledger_tail,
        "memory_facts": [
            fact
            for fact in memory_facts
            if fact.get("source") in {"tool-verified", "human-approved"}
        ],
    }
