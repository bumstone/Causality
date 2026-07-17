# Spec 005 — Workflow and Debug Loop

Status: implemented.

## Contract

Selected playbooks are frozen as ordered task phases. Runtime owns transitions
and evidence gates; the host agent supplies reasoning and edits.

## Wire

- MCP `begin` defaults `workflow` to `auto`; callers may select
  `root-cause-protocol`. Omitted legacy retries remain compatible.
- `causality_task_phase` starts the current `phase_id` or finishes it with
  `passed|failed|blocked` and task-scoped evidence hashes.
- `causality_task_hypothesis` records hypothesis, verifier, rationale,
  `supported|rejected|inconclusive`, and evidence.
- `approve(stage=phase)` needs the matching phase, trusted proof, and exact
  `approval_evidence_refs` from the blocked task.
- Same key+digest replays. Conflicts and malformed input are zero-write.
  Interrupted blocks recover only through exact phase/hypothesis replay.

## Invariants

- Phase state is `pending|running|passed|failed|blocked` with stable ID, attempt,
  requirements, and evidence.
- Root cause order: reproduce → hypothesis → verify → fix.
- Actions need a running phase. Passing needs fresh local work, required
  verification, and two independent pass verdicts.
- The frozen failed-hypothesis limit (three by default) blocks effects and
  exposes its operation hashes plus escalation gate hash for HITL.
- Terminal rejection is not recoverable; historical exact replays are zero-write.
- Reflection deduplicates by task, phase, normalized verifier/rationale, and scope
  while retaining first time and provenance.

## Acceptance

Unit/MCP tests cover routing, order, refusal, crash recovery, HITL, and dedup.
A fresh-venv installed-package E2E proves a real fail→pass target across restart,
four completed phases, complete/reflect replay, ledger validity, and one memory
failure per cause.
