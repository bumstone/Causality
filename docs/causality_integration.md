# Causality Integration Structure

Delivery: [Plan 001](plans/001-external-harness-delivery.md). Interfaces:
[spec index](specs/README.md).

## Runtime

| Module | Responsibility |
| --- | --- |
| `GoalContract` | risk, permissions, evidence, checks, stop policy |
| `TaskLifecycle` | durable task/phase policy, allowed-next, replay, recovery |
| `PlaybookPhase` | stable ordered phase requirements |
| `EvidenceLedger` | append-only hash chain and artifact provenance |
| `HITLGate` | plan/action/completion policy |
| `HttpAdapter` | bounded transport behind task gates |
| `A11yBrowserAdapter` | isolated protocol-v1 browser primitives |

## State policy

```text
planned -> approved -> executing -> verified
                   -> blocked | rejected
phase: pending -> running -> passed | failed | blocked
```

| State | Required basis |
| --- | --- |
| planned | frozen contract and phase plan |
| executing | current phase plus permitted action |
| passed phase | fresh work/verification and two local verdicts |
| blocked | stop threshold, uncertain effect, or escalation evidence |
| verified | all phases, current checks, two passes, final HITL when required |
| rejected | human rejection; terminal historical replays only |

The frozen failed-hypothesis limit (three by default) blocks effects.
`approval_evidence_refs` binds phase HITL to the rejection streak and gate.
Process loss is repaired by replaying the exact phase/hypothesis request before
approval.

## Boundaries

Server policy is the authority ceiling; contracts only narrow it. Paths/cwd stay
inside the project. Commands use argv, never shell strings. HTTP uses exact
origins/auth aliases. Browser text and driver output are untrusted; raw profiles
stay outside prompt context. Completion/reflection derive from ledger evidence,
not agent prose.
