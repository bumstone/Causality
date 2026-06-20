"""Reflect step: distill a contract's ledger trail into typed memory (ADR 0006 §6.1-3).

After a run, the append-only :class:`EvidenceLedger` (L4) holds the raw trail of
evidence, verifier decisions, gate decisions, and human decisions. The Reflect
step reads only the events for one ``GoalContract`` and *distills* them into
long-term typed memory (L0): a single ``retrospectives`` summary plus one
``failures`` entry per failure signal (ADR 0006 §2.1 evolution loop).

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

from dataclasses import dataclass

from .contracts import AuditEventType, GateDecision, GoalContract
from .ledger import EvidenceLedger, LedgerEvent
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


def reflect_on_contract(
    ledger: EvidenceLedger,
    memory: TypedMemory,
    contract: GoalContract,
    *,
    failure_scope: str | None = None,
) -> Reflection:
    """Distill ``contract``'s ledger trail into typed long-term memory.

    Reads only events whose ``contract_id`` matches the contract, records one
    ``retrospectives`` summary of the run, and one ``failures`` entry per
    failure signal (critical-or-not verifier ``fail`` decisions and ``repair``
    gate decisions). Returns the :class:`Reflection` it wrote.

    ``failure_scope`` is the scope stamped on the recorded ``failures``. It
    defaults to ``contract:<goal_id>`` -- unique per run, so failures stay tied
    to their own contract. A caller that wants failures to feed forward as
    guardrails for *future* runs passes a stable scope (e.g. a task family), so
    the next run in that scope can recall them (see ``CausalityEngine.run_task``
    ``failure_scope``/``confirm_guardrails``).
    """
    events = ledger.events_for_contract(contract.goal_id)

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
    provenance = ledger.latest_hash_for_contract(contract.goal_id)
    retrospective = memory.record(
        "retrospectives",
        summary,
        provenance=provenance,
    )

    scope = failure_scope or f"contract:{contract.goal_id}"
    failures: list[MemoryEntry] = []

    for event in verifier_fails:
        verifier = event.payload.get("verifier", "unknown")
        rationale = event.payload.get("rationale", "")
        critical = event.payload.get("severity") == "critical"
        marker = "critical verifier failure" if critical else "verifier failure"
        failure_summary = f"{marker} from '{verifier}': {rationale}".strip()
        failures.append(
            memory.record_failure(
                failure_summary,
                scope=scope,
                provenance=event.entry_hash,
            )
        )

    for event in gates:
        if event.payload.get("decision") != GateDecision.REPAIR.value:
            continue
        reasons = event.payload.get("reasons") or []
        reason = reasons[0] if reasons else "repair required"
        failures.append(
            memory.record_failure(
                f"repair gate decision: {reason}",
                scope=scope,
                provenance=event.entry_hash,
            )
        )

    return Reflection(retrospective=retrospective, failures=tuple(failures))
