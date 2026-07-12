# Spec 006 — Resume and Skill Operations

Status: in progress — 006A resume/context implemented; 006B skill operations planned.

## Contract

Interrupted tasks resume from durable session and ledger state without repeating
terminal effects. Earned skills remain local, evidence-backed, and HITL-promoted.

## Behavior

- `causality_task_resume(task_id)` is a closed, read-only projection. It rebuilds
  the frozen contract, phase, unmet verification freshness, hypothesis count,
  safe pending-intent metadata, and allowed next actions from `TaskSession` plus
  the chain-verified ledger. It does not accept an idempotency key or append a
  resume event.
- Terminal or reflected tasks return their recorded result without re-running
  actions, reflection, or distillation.
- An action intent without a result becomes `blocked` and needs human resolution;
  resume never guesses whether it ran.
- Resume is status/recovery guidance, not an automatic effect replay API. The
  caller must retain and re-submit an exact safe operation request when one is
  needed; uncertain external actions expose only the human `resolve` route.
- `causality_skill_outcome` records reproducibility attempts.
  `causality_skill_promote` needs named approval, minimum attempts/successes, and
  authored-skill deduplication.
- Context chain-verifies before returning a metadata-only ledger tail, returns
  active failures only with TTL, lists curated Markdown paths, and labels the
  recommended ignore patterns for local runtime JSONL without claiming that a
  host repository's `.gitignore` was modified.

## Acceptance

006A acceptance covers unit, rotated-ledger, and installed external-project
stdio restart tests: a phase resumes without effects, terminal/reflection
results replay as recorded, action descriptors and secrets remain hidden,
runtime JSONL does not stale verification while curated Markdown does, TTL is
honored, and tampered chains fail closed. 006B additionally covers skill
outcome idempotency, reproducibility thresholds, authored-skill deduplication,
and HITL promotion.
