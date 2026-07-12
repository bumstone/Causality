# Causality Project Summary

Baseline: `414` tests pass; Git and ledger retain commit history.

## What It Is

Causality is a local-first agent control harness. It binds a goal contract,
gates execution, records evidence, and reflects verified outcomes.

## Current Truth

| Area | Status |
| --- | --- |
| Dispatch | Each task type maps to a vendored playbook. |
| Contract | `GoalContract` freezes objective, non-goals, tools, and verification into `TaskContract`. |
| Gates | `run_task`, `ExecutionAdapter`, and `ToolAdapter` enforce plan/action/tool/non-goal checks. |
| Completion | Evidence and substantive independent verifier passes are required. |
| Verification | Frozen argv runs without a shell; evidence and citations bind completion. |
| Ledger | Durable hash-chained JSONL with repair, cache, rotation, and offset paging. |
| Feedback | Approved failures can become TTL-bounded later non-goals. |
| Skills | Earned skills can be distilled, promoted, deduped, recalled, and injected. |
| Redaction | Skill distillation masks sensitive keys, nested secrets, tokens, and auth headers. |
| Install | Client-native MCP config, host-safe adoption, handshake/report; context omits raw ledger payloads and paths reject symlink escape. |
| MCP lifecycle | Closed tools persist task actions, verification, completion, recovery, and reflection. |

## Still Not Proven

- API/browser execution through the file/subprocess contract path.
- Engine execution of vendored playbook phases; they currently guide the agent.
- Durable MCP resume and earned-skill outcome/promotion operations.

## Sources

| Need | Source |
| --- | --- |
| Status | `docs/project-summary.md`, `docs/status/roadmap.html` |
| Delivery order | `docs/plans/README.md` |
| Feature contracts | `docs/specs/README.md` |
| Decisions | `docs/adr/*.md` |
| Workflows/templates | `src/causality/workflows.py`, `agent_bootstrap.py` |
| Runtime/tests | `src/causality/*.py`, `tests/*.py` |
