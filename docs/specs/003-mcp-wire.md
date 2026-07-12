# Spec 003 — MCP Wire Contract

Status: implemented. `tools/list.inputSchema` is normative and closed;
validation precedes ledger writes.

## Inputs

Mutations require nonblank `idempotency_key`; follow-ups require `task_id`.
`begin` requires objective, risk, permissions, verification requirements, stop
condition, and key.

| Tool | Extra fields (`?` optional) |
| --- | --- |
| approve | stage, approved, approver, rationale, evidence_refs, proof |
| action | action; subprocess cwd?, timeout_seconds? |
| verify | requirement_id, mode; manual fields below |
| verdict | verifier, status, rationale, evidence_refs, severity? |
| complete | none |
| resolve | operation_id, resolution, approver, rationale, proof |
| reflect | scope?, ttl_days? |
| append_evidence | kind, payload, artifact_paths? |

Manual verify also requires evidence_hash, approved, approver, rationale, proof.
Arrays are never string-coerced; text values are nonblank. Resolution is
`applied|not_applied|reject`; only `not_applied` reopens.

## Outputs and state

Success: `{ok,task,event_hash,idempotency:{key,replayed},data}`. Failure sets
`isError`: `{ok:false,error:{code,message,retryable,details},task?}`. Same
key+digest replays. Complete maps PASS→verified, REPAIR→executing,
ESCALATE/STOP→blocked. Terminal states never reopen.

## Error triggers

- `validation_error`: shape/type/enum/blank failure; no write.
- `policy_denied`: request exceeds frozen policy.
- `approval_required`: untrusted proof; default deny.
- `task_blocked|unresolved_action_intent`: resolve uncertain work.
- `recovery_in_progress`: another decision owns the effect.
- `completion_snapshot_stale`: PASS snapshot has a later task event or current
  workspace fingerprint differs; retry needs fresh evidence and a new key.
- `task_terminal`: new work after terminal.

Partial ESCALATE/STOP and rejection decisions block effects immediately.
Bad stdio is recoverable; notifications have no response.
