# Causality Project Summary

Baseline: `296` tests green (PR #35 post-review verification). Git and the
evidence ledger retain commit-specific history.

## What It Is

Causality is a local-first control harness for agent work. It binds a goal
contract, gates execution, records evidence, then reflects verified outcomes
into memory and skills.

## Current Truth

| Area | Status |
| --- | --- |
| Dispatch | Each task type resolves to a structured vendored playbook. |
| Contract | `GoalContract` freezes objective, non-goals, tools, and verification into `TaskContract`. |
| Gates | `run_task`, `ExecutionAdapter`, and `ToolAdapter` enforce plan/action/tool/non-goal checks. |
| Completion | Evidence and substantive independent verifier passes are required. |
| Ledger | Durable hash-chained JSONL with repair, cache, rotation, and offset paging. |
| Feedback | Approved failures can become TTL-bounded later non-goals. |
| Skills | Earned skills can be distilled, promoted, deduped, recalled, and injected. |
| Redaction | Skill distillation masks sensitive keys, nested secrets, tokens, and auth headers. |
| Install | Client-native MCP config, host-safe adoption, handshake/report; context omits raw ledger payloads and paths reject symlink escape. |

## Still Not Proven

- Execution of declared verification argv with requirement IDs and hashes.
- Durable create/approve/act/verify/complete task lifecycle over MCP.
- API/browser execution through the file/subprocess contract path.
- Engine execution of vendored playbook phases; they currently guide the agent.

## Sources

| Need | Source |
| --- | --- |
| Status | `docs/project-summary.md`, `docs/status/roadmap.html` |
| Delivery order | `docs/plans/README.md` |
| Feature contracts | `docs/specs/README.md` |
| Decisions | `docs/adr/*.md` |
| Workflows/templates | `src/causality/workflows.py`, `agent_bootstrap.py` |
| Runtime/tests | `src/causality/*.py`, `tests/*.py` |
