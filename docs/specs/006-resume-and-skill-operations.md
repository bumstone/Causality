# Spec 006 — Resume and Skill Operations

Status: planned.

## Contract

Interrupted tasks resume from durable session and ledger state without repeating
terminal effects. Earned skills remain local, evidence-backed, and HITL-promoted.

## Behavior

- `causality_task_resume(task_id)` rebuilds contract, phase, unmet verification,
  hypothesis count, and allowed next actions from `TaskSession` plus ledger.
- Terminal or reflected tasks return their recorded result without re-running
  actions, reflection, or distillation.
- An action intent without a result becomes `blocked` and needs human resolution;
  resume never guesses whether it ran.
- `causality_skill_outcome` records reproducibility attempts.
  `causality_skill_promote` needs named approval, minimum attempts/successes, and
  authored-skill deduplication.
- Context returns active failures only, honors TTL, and distinguishes curated
  Markdown from ignored runtime JSONL.

## Acceptance

Restart tests resume midway through a phase, return terminal results without
effects, honor TTL, and prevent promotion without reproducibility and HITL.
