# Spec 006 — Resume and Skill Operations

## Contract

Interrupted tasks resume from durable session and ledger state without repeating
terminal effects. Earned skills remain local, evidence-backed, and HITL-promoted.

## Behavior

- `causality_task_resume(task_id)` rebuilds contract, phase, unmet verification,
  hypothesis count, and allowed next actions from `TaskSession` plus ledger.
- Terminal or reflected tasks return their recorded result and do not re-run
  actions, reflection, or distillation.
- An action intent without a terminal result after process loss becomes `blocked`
  and requires human resolution; resume never guesses whether it ran.
- `causality_skill_outcome` records reproducibility attempts; `causality_skill_promote`
  requires named human approval, minimum attempts/successes, and authored-skill
  deduplication.
- Context returns active failures only, honoring TTL, and distinguishes curated
  Markdown assets from ignored runtime JSONL state.

## Acceptance

Restart-process tests resume midway through a phase, reject terminal replays,
honor TTL, and prove promotion cannot occur without reproducibility and HITL.
