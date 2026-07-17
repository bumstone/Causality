# Causality Project Summary

Baseline: `538` tests pass; Git and ledger retain commit history.

## What It Is

Local-first agent control: contract, gated execution, evidence, and reflection.

## Current Truth

| Area | Status |
| --- | --- |
| Dispatch | Task intent resolves to structured vendored playbooks. |
| Contract | `GoalContract` freezes objective, non-goals, tools, checks, and stops. |
| Workflow | Selected phases persist with order, attempts, evidence, and allowed next actions. |
| Debug loop | The frozen rejection limit blocks effects until evidence-backed HITL. |
| Completion | Fresh verification and two substantive independent passes are required. |
| Ledger | Hash-chained JSONL supports repair, rotation, indexing, and restart replay. |
| Reflection | Stable causes deduplicate while first time and provenance are retained. |
| Skills | Local APIs distill, promote, dedupe, recall, and inject earned skills. |
| Install | Host-owned rules, native MCP config, safe adoption, handshake, and activation reports work. |
| MCP | Closed tools cover phases, actions, HTTP/browser, verify, complete, and reflect. |
| HTTP/browser | Exact scopes, approvals, redaction, state binding, bounded secret transport, artifacts, and replay are enforced. |

## Still Not Proven

- General `task_resume`/result lookup for interrupted and terminal tasks.
- MCP skill outcome and HITL promotion operations; current skill APIs are local.
- Automatic orchestration across install, workflow selection, recovery, verification,
  and reflection as one external-client-owned loop (planned as Spec 007).
- External App, secret, repository-rule, and autofix operating configuration.

## Sources

| Need | Source |
| --- | --- |
| Delivery order | `docs/plans/README.md` |
| Feature contracts | `docs/specs/README.md` |
| Decisions | `docs/adr/*.md` |
| Runtime/tests | `src/causality/*.py`, `tests/*.py` |
