# Spec 003 — MCP Wire Contract

Status: implemented.

## Inputs

Mutations use `idempotency_key`; follow-ups use `task_id`. Objects are closed.
`begin` adds objective, risk, permissions, checks, stop condition, and optional
`workflow=auto|root-cause-protocol`.

| Tool | Extra fields (`?` optional) |
| --- | --- |
| approve | stage, approved, approver, rationale, evidence_refs, proof, phase_id? |
| phase | phase_id, action; finish adds status, evidence_refs |
| hypothesis | phase_id, hypothesis, verifier, status, rationale, evidence_refs |
| action | typed action; cwd?, timeout? |
| http/browser | closed Spec 004 fields |
| verify | requirement_id, mode; manual decision fields when manual |
| verdict | verifier, status, rationale, evidence_refs, severity? |
| complete | none |
| resolve | operation_id, resolution, approver, rationale, proof |
| reflect | scope?, ttl_days? |

Text is nonblank; arrays remain arrays. Only `not_applied` resolution reopens an
uncertain effect.

## Outputs and state

Success: `{ok,task,event_hash,idempotency:{key,replayed},data}`. Domain errors set
`isError` and return `{ok:false,error:{code,message,retryable,details},task?}`.
Exact retries return recorded data even when replay repairs a transition.

`task` exposes state, current/ordered phases, `allowed_next`, block reason, and
approval evidence. Terminal tasks permit recorded replays and reflection.

## Failure rules

- `validation_error|idempotency_conflict`: bad/conflicting input; no write.
- `policy_denied|approval_required`: outside authority or untrusted proof.
- `phase_mismatch|phase_evidence_*`: stale phase or incomplete evidence.
- `recovery_required`: replay the interrupted phase operation before HITL.
- `task_blocked|unresolved_action_intent`: resolve uncertain work.
- `completion_snapshot_stale`: ledger/workspace changed after PASS.
- `task_terminal`: new terminal work is forbidden.

Bad stdio is recoverable; notifications have no response. Ledger state is
authoritative.
