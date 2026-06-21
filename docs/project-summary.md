# Causality Project Summary

Baseline: PR #29, merge `6c239e0`, `231` tests green.

## What It Is

Causality is a local-first control harness for agent work. A run is bound to a
goal contract, gated, recorded in a durable ledger, then reflected into memory
and skills. The point is auditability: claims need evidence.

## Current Truth

| Area | Status |
| --- | --- |
| Contract | `GoalContract` freezes into `TaskContract`: objective, non-goals, tools, verification. |
| Gates | `run_task`, `ExecutionAdapter`, and `ToolAdapter` enforce plan/action/tool/non-goal checks. |
| Completion | Required evidence + substantive verifier passes; blank/hollow passes do not count. |
| Ledger | Hash-chained JSONL, durable writes, locks, torn-tail repair, read cache. |
| Feedback | Approved failures can become later non-goals; TTL prevents permanent ratchets. |
| Skills | Earned skills can be distilled, promoted, deduped, recalled, and injected into execution. |
| Redaction | Distilled skills mask sensitive keys, nested structures, token shapes, and auth headers. |

## Still Not Proven

- Broader E2E scenarios across feedback, skill recall, review, completion, and tool adapter use.
- API/browser playbooks beyond the bundled file/subprocess adapter.
- Repo automation health checks for PR review/autofix app and secret setup.
- Ledger rotation/archive/`.idx` policy once real scale justifies it.

## Sources

| Need | Source |
| --- | --- |
| Current status | `docs/project-summary.md`, `docs/status/roadmap.html` |
| Decisions | `docs/adr/*.md` |
| Workflow definitions | `src/causality/workflows.py` |
| Installed agent templates | `src/causality/agent_bootstrap.py` |
| Runtime behavior | `src/causality/*.py` |
| Regression | `tests/*.py` |

## Cleanup Guidance

Do not delete passing tests. They protect verifier quorum, redaction, ledger
durability, routing fail-safes, and feedback loops. Future cleanup should split
large modules/tests and table-drive repeated cases, not remove coverage.
