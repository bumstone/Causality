"""Reflect step: distill a contract's ledger trail into typed memory (ADR 0006 §6.1-3).

After a run, the append-only :class:`EvidenceLedger` (L4) holds the raw trail of
evidence, verifier decisions, gate decisions, and human decisions. The Reflect
step reads only the events for one ``GoalContract`` and *distills* them into
long-term typed memory (L0): a single ``retrospectives`` summary plus one
``failures`` entry per stable failure cause (ADR 0006 §2.1 evolution loop).

Distillation, not transcription (ADR 0007 "completion -> typed summary only"):
the retrospective is one concise paragraph of counts, not a replay of events.

Governance (ADR 0005 §2.5): Reflect only records ``retrospectives`` and
``failures``. It never writes to ``decisions`` and never promotes anything, so a
failed or assumed judgement cannot be laundered into durable knowledge here. A
decision must still travel the explicit promotion gate with confirming evidence.
Every memory entry carries ``provenance`` (a ledger ``entry_hash``) so its chain
of custody back to raw evidence survives the L0 boundary (ADR 0005 §2.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .contracts import AuditEventType, GateDecision, GoalContract
from .ledger import EvidenceLedger, LedgerEvent, sha256_text
from .memory import MemoryEntry, TypedMemory


@dataclass(frozen=True)
class Reflection:
    """The typed-memory result of reflecting on one contract's ledger trail."""

    retrospective: MemoryEntry
    failures: tuple[MemoryEntry, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "retrospective": self.retrospective.to_dict(),
            "failures": [entry.to_dict() for entry in self.failures],
        }


def _is_pass(event: LedgerEvent) -> bool:
    return event.payload.get("status") == "pass"


def _is_fail(event: LedgerEvent) -> bool:
    return event.payload.get("status") == "fail"


def _normalized(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _event_phases(events: list[LedgerEvent]) -> dict[str, str]:
    phases: dict[str, str] = {}
    current = ""
    for event in events:
        response = event.payload.get("response")
        phase = response.get("phase") if isinstance(response, dict) else None
        if (
            event.event_type == AuditEventType.TASK_OPERATION.value
            and event.payload.get("operation") == "phase"
            and isinstance(phase, dict)
        ):
            phase_id = phase.get("phase_id")
            status = phase.get("status")
            if status == "running" and isinstance(phase_id, str):
                current = phase_id.strip()
            phases[event.entry_hash] = current
            if status in {"passed", "failed", "blocked"}:
                current = ""
            continue
        phases[event.entry_hash] = current
    return phases


def _failure_id(
    task_id: str,
    phase_id: str,
    verifier: str,
    rationale: str,
    scope: str,
) -> str:
    return sha256_text(
        json.dumps(
            ["failure-cause-v1", task_id, phase_id, verifier, rationale, scope],
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )


def reflect_on_contract(
    ledger: EvidenceLedger,
    memory: TypedMemory,
    contract: GoalContract,
    *,
    failure_scope: str | None = None,
    failure_ttl_days: int | None = None,
    reflection_id: str | None = None,
    created_at: str | None = None,
    source_event_hash: str | None = None,
) -> Reflection:
    """Distill ``contract``'s ledger trail into typed long-term memory.

    Reads only events whose ``contract_id`` matches the contract, records one
    ``retrospectives`` summary of the run, and one ``failures`` entry per stable
    cause found in verifier ``fail`` decisions and ``repair`` gate decisions.
    Returns the :class:`Reflection` it wrote.

    ``failure_scope`` is the scope stamped on the recorded ``failures``. It
    defaults to ``contract:<goal_id>`` -- unique per run, so failures stay tied
    to their own contract. A caller that wants failures to feed forward as
    guardrails for *future* runs passes a stable scope (e.g. a task family), so
    the next run in that scope can recall them (see ``CausalityEngine.run_task``
    ``failure_scope``/``confirm_guardrails``).

    ``failure_ttl_days`` stamps a TTL on the recorded failures so a fed-forward
    guardrail expires from ``entries(active_only=True)`` instead of being offered
    forever; ``None`` records no TTL (the failure persists until swept/revoked).
    """
    events = ledger.events_for_contract(contract.goal_id, all_segments=True)
    if reflection_id is not None:
        if not created_at or not source_event_hash:
            raise ValueError(
                "deterministic reflection requires created_at and source_event_hash"
            )
        cutoff = next(
            (index for index, event in enumerate(events) if event.entry_hash == source_event_hash),
            None,
        )
        if cutoff is None:
            raise ValueError("reflection source_event_hash is not contract-scoped")
        events = events[: cutoff + 1]

    evidence = [e for e in events if e.event_type == AuditEventType.EVIDENCE.value]
    verifiers = [e for e in events if e.event_type == AuditEventType.VERIFIER_DECISION.value]
    gates = [e for e in events if e.event_type == AuditEventType.GATE_DECISION.value]
    humans = [e for e in events if e.event_type == AuditEventType.HUMAN_DECISION.value]

    verifier_passes = [e for e in verifiers if _is_pass(e)]
    verifier_fails = [e for e in verifiers if _is_fail(e)]

    gate_counts = {decision.value: 0 for decision in GateDecision}
    for event in gates:
        decision = event.payload.get("decision")
        if decision in gate_counts:
            gate_counts[decision] += 1

    human_approvals = sum(1 for e in humans if e.payload.get("approved") is True)

    summary = (
        f"Reflect on contract {contract.goal_id} ({contract.title}): "
        f"{len(evidence)} evidence event(s); "
        f"verifier {len(verifier_passes)} pass / {len(verifier_fails)} fail; "
        f"gate decisions pass={gate_counts[GateDecision.PASS.value]}, "
        f"repair={gate_counts[GateDecision.REPAIR.value]}, "
        f"escalate={gate_counts[GateDecision.ESCALATE.value]}, "
        f"stop={gate_counts[GateDecision.STOP.value]}; "
        f"{human_approvals} human approval(s)."
    )
    # Provenance must be contract-scoped: the last event for THIS contract, not
    # ledger.latest_hash() which, in interleaved multi-contract runs, may belong
    # to another contract and break the audit trail (codex review r3382219479).
    provenance = source_event_hash or ledger.latest_hash_for_contract(contract.goal_id)
    if reflection_id is None:
        retrospective = memory.record(
            "retrospectives",
            summary,
            provenance=provenance,
        )
    else:
        retrospective = memory.record_once(
            "retrospectives",
            summary,
            entry_id=sha256_text(f"{reflection_id}:retrospective"),
            created_at=created_at,
            provenance=provenance,
        )

    scope = failure_scope or f"contract:{contract.goal_id}"
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("failure_scope must be non-blank")
    failures: list[MemoryEntry] = []
    phases = _event_phases(events)
    signals: dict[str, tuple[LedgerEvent, str, str]] = {}
    for event in events:
        if event.event_type == AuditEventType.VERIFIER_DECISION.value and _is_fail(event):
            verifier_value = event.payload.get("verifier", "unknown")
            rationale_value = event.payload.get("rationale", "")
            critical = event.payload.get("severity") == "critical"
            marker = "critical verifier failure" if critical else "verifier failure"
            failure_summary = (
                f"{marker} from '{verifier_value}': {rationale_value}"
            ).strip()
        elif (
            event.event_type == AuditEventType.GATE_DECISION.value
            and event.payload.get("decision") == GateDecision.REPAIR.value
        ):
            reasons = event.payload.get("reasons") or []
            rationale_value = reasons[0] if reasons else "repair required"
            verifier_value = "gate:repair"
            failure_summary = f"repair gate decision: {rationale_value}"
        else:
            continue
        phase_id = phases[event.entry_hash]
        entry_id = _failure_id(
            contract.goal_id,
            phase_id,
            _normalized(verifier_value),
            _normalized(rationale_value),
            scope,
        )
        signals.setdefault(entry_id, (event, failure_summary, phase_id))

    for entry_id, (event, failure_summary, phase_id) in signals.items():
        metadata: dict[str, Any] = {"scope": scope, "phase_id": phase_id}
        if failure_ttl_days is not None:
            metadata["ttl_days"] = failure_ttl_days
        failures.append(
            memory.record_once(
                "failures",
                failure_summary,
                entry_id=entry_id,
                created_at=event.timestamp,
                provenance=event.entry_hash,
                **metadata,
            )
        )

    return Reflection(retrospective=retrospective, failures=tuple(failures))
