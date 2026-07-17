# Spec 006 — Resume and Skill Operations

Status: in progress — 006A implemented; 006B planned.

## Contract

Interrupted tasks resume from durable session and ledger state without repeating
terminal effects. Earned skills remain local, evidence-backed, and HITL-promoted.

## Behavior

- `causality_task_resume(task_id)` is a closed, read-only projection. It rebuilds
  the frozen contract, phase, fresh unmet verification, hypothesis count, safe
  pending-intent metadata, and allowed next actions from `TaskSession` plus the
  chain-verified ledger. It accepts no idempotency key and appends no event.
- Terminal or reflected tasks return their recorded result without re-running
  actions, reflection, or distillation.
- An action intent without a result becomes `blocked` and needs human resolution;
  resume never guesses whether it ran.
- Resume is status/recovery guidance, not automatic effect replay. The caller
  re-submits an exact safe request; uncertain external actions expose only the
  human `resolve` route.
- `causality_skill_outcome` records reproducibility attempts.
  `causality_skill_promote` needs named approval, minimum attempts/successes, and
  authored-skill deduplication.
- Context chain-verifies before a metadata-only ledger tail, returns TTL-active
  failures, lists curated Markdown paths, and labels recommended local JSONL
  ignore patterns without claiming to modify a host `.gitignore`.

## Acceptance

006A acceptance covers unit, rotated-ledger, and installed external-project
stdio restart tests: phase status resumes without effects; terminal/reflection
results replay; descriptors/secrets stay hidden; runtime JSONL does not stale
verification while Markdown does; TTL and tamper fail-closed checks pass. 006B
covers skill idempotency, reproducibility thresholds, deduplication, and HITL.
