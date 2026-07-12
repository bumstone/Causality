# Spec 005 — Workflow and Debug Loop

## Contract

Workflow templates and playbooks become persisted phase runners. Automation
selects the next permitted phase; agents still supply reasoning and code edits.

## Behavior

- Persist phase state: `pending`, `running`, `passed`, `failed`, `blocked`, with
  phase ID and evidence hashes.
- Map `debug`, `debugging`, and `diagnose` to the implementation/debug bundle;
  explicit `root-cause-protocol` selects reproduce → hypothesis → verify → fix.
- `causality_task_hypothesis` records `supported|rejected|inconclusive`.
  Three rejected hypotheses escalate to HITL before another fix action.
- A phase advances only after required action/verification evidence and verdicts.
- Reflect deduplicates failures by task, phase, verifier, normalized rationale,
  and scope; loop repetition cannot create duplicate memory entries.

## Acceptance

Tests prove debug routing, phase ordering, three rejected hypotheses, no advance
without evidence, and one memory failure per unique cause.
