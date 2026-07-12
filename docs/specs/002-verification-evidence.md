# Spec 002 — Verification Evidence

## Contract

Declared verification is executable, not a free-text evidence-kind label. A
task completes only when every required requirement has a fresh, passing,
task-scoped result and independent substantive verifier passes.

## Data and migration

Add `VerificationRequirement`: `id`, `argv`, `expected_exit_codes`,
`timeout_seconds`, `artifact_paths`, and `required`. Store it in `GoalContract`.
Keep legacy `Sequence[str]` only for one minor release: convert each string to a
unique requirement ID and emit `DeprecationWarning`; then remove it.

`causality_task_verify` executes argv without a shell through the action gate.
It records requirement ID, argv, exit code, stdout/stderr sizes, artifact hashes,
event hash, and completion timestamp. Manual checks require `manual: true`, an
evidence hash, and a human verdict; they never satisfy executable requirements.

## Completion

Completion rejects missing IDs, failed exits, stale results, blank/foreign event
hashes, duplicate verifier names, or missing final approval. It accepts only the
latest result produced after the task's last relevant mutation and reports each
unmet requirement.

## Acceptance

- Nonexistent command, nonzero exit, wrong artifact, or cross-task evidence
  cannot complete a task.
- A passing command plus two independent cited verdicts completes it.
- Timeout and blocked tool results are ledger evidence and leave the task blocked.
