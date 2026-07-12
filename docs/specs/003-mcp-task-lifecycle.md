# Spec 003 — MCP Task Lifecycle

## Contract

MCP exposes a durable task lifecycle using Spec 002 requirements. It does not
accept Python callbacks or claim to reason/edit code itself. The host agent
chooses actions; Causality binds, gates, records, and terminates them.

## Tools

Keep `causality_init`, `causality_context`, and `causality_workflows`. Add:

1. `causality_task_begin`: task input → `task_id`, `contract_id`, `planned`.
2. `causality_task_approve`: records required plan approval.
3. `causality_task_action`: typed file/subprocess action through `ToolAdapter`.
4. `causality_task_verdict`, `causality_task_complete`, `causality_task_reflect`.

Deprecate unrestricted `causality_append_evidence`; require `task_id`, schema
validation, and return event hash plus task state.

## Persistence and safety

`TaskSession` stores schema version, immutable contract snapshot/ID, state,
phase, requirement results, hypothesis count, idempotency keys, reflection flag,
and event hashes. Ledger is source of truth; a session must be reconstructible.
States: `planned`, `approved`, `executing`, `verified`, `blocked`, `rejected`.

Write an action-intent event before an external action. On restart, an unresolved
intent becomes `blocked` for human resolution, never an automatic replay.

## Acceptance

JSON-RPC tests cover valid/invalid transitions, blocked tools, high-risk approval,
restart recovery, idempotent reflection, and action replay prevention.
