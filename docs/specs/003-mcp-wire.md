# Spec 003 — MCP Wire Contract

Status: implemented.

## Inputs

Mutations need a nonblank key; follow-ups need `task_id`. `begin` also needs
objective, risk, permissions, verification requirements, and stop condition.

Begin evidence kinds are `test_output|browser_diff|artifact_hash|tool_output|a11y_report|verification_result`.
`append_evidence` produces the first five;
`verify` produces the last.

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

Manual verify needs evidence_hash, approved, approver, rationale, and proof.
Arrays stay arrays; text is nonblank. Resolution is
`applied|not_applied|reject`; only `not_applied` reopens.

## Outputs and state

Success: `{ok,task,event_hash,idempotency:{key,replayed},data}`. Errors set
`isError` and return `{ok:false,error:{code,message,retryable,details},task?}`.
Same key+digest replays. Complete maps PASS→verified, REPAIR→executing, and
ESCALATE/STOP→blocked. Terminal states never reopen.

## Error triggers

- `validation_error`: invalid shape/type/enum/blank; no write.
- `policy_denied`: request exceeds frozen policy.
- `approval_required`: untrusted proof; default deny.
- `task_blocked|unresolved_action_intent`: resolve uncertain work.
- `recovery_in_progress`: another decision owns the effect.
- `completion_snapshot_stale`: PASS snapshot has a later task event or current
  workspace differs; retry needs fresh evidence and a new key.
- `task_terminal`: new work after terminal.

Partial ESCALATE/STOP and rejection block effects. Bad stdio is recoverable;
notifications have no response.
