# Causality Integration Structure

Planned delivery order: [Plan 001](plans/001-external-harness-delivery.md).
Interfaces and acceptance tests: [spec index](specs/README.md).

## Runtime

| Module | Responsibility |
| --- | --- |
| `GoalContract` | risk, permissions, evidence, state, stop policy |
| `EvidenceLedger` | append-only hash chain and artifact hashes |
| `HITLGate` | plan/action/completion policy enforcement |
| `HttpAdapter` | bounded no-redirect transport behind task gates |
| `A11yBrowserAdapter` | bounded protocol-v1 primitives behind task lifecycle gates |
| `WorkflowTemplate` | planning, subagent, verification, TDD, root-cause contracts |

## State policy

```text
planned -> approved -> executing -> verified
                   \-> blocked | rejected
```

| State | Required evidence |
| --- | --- |
| planned | goal contract |
| approved | plan gate or human approval |
| executing | gated action approval |
| verified | required evidence, two independent passes, final HITL when required |
| blocked | no progress, failed hypotheses, missing context, or escalation |
| rejected | human rejection or unresolved critical policy failure |

## HITL

Require approval for high-risk plans/contracts, irreversible actions, delete,
deploy, payment, external send, permission changes, critical verifier conflict,
evidence waiver, and high-risk final acceptance. Record stage, approver,
rationale, and raw artifact references.

## Browser observations

Each task uses a private session/profile. Raw browser state stays in an ignored,
hash-verified cache; MCP wraps it as untrusted data. Supply only needed
URL/title/viewport, compact or interactive A11y tree, stable refs, canonical
state hash, action diff, console/network hashes, and screenshot/report refs.
Page text and driver output are untrusted.

Escalate in order: compact snapshot → scoped subtree → attributes/HTML →
annotated screenshot → human review.
