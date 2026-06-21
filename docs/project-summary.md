# Causality Project Summary

Baseline: `251` green (#32 `e78ec21` + vendored L1 playbooks).

## What It Is

Causality is a local-first control harness for agent work. A run is bound to a
goal contract, gated, recorded in a durable ledger, then reflected into memory
and skills. The point is auditability: claims need evidence.

## Current Truth

| Area | Status |
| --- | --- |
| Dispatch | L1 routes each task type to one vendored playbook bundle; every routed label resolves to structured phases or raises. |
| Contract | `GoalContract` freezes into `TaskContract`: objective, non-goals, tools, verification. |
| Gates | `run_task`, `ExecutionAdapter`, and `ToolAdapter` enforce plan/action/tool/non-goal checks. |
| Completion | Required evidence + substantive verifier passes; blank/hollow passes do not count. |
| Ledger | Hash-chained JSONL, durable writes, locks, torn-tail repair, read cache, opt-in chain-verifiable rotation with offset-indexed paging. |
| Feedback | Approved failures can become later non-goals; TTL prevents permanent ratchets. |
| Skills | Earned skills can be distilled, promoted, deduped, recalled, and injected into execution. |
| Redaction | Distilled skills mask sensitive keys, nested structures, token shapes, and auth headers. |

## Still Not Proven

- API/browser execution beyond the file/subprocess `ToolAdapter`; the vendored
  playbook phases are guidance the agent follows, not engine-auto-executed.

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

Don't delete passing tests -- they protect verifier quorum, redaction, ledger
durability, routing fail-safes, and feedback loops. Split/table-drive rather
than remove coverage.
