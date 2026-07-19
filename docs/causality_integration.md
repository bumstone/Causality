# Causality Integration

Order: [Plan 001](plans/001-external-harness-delivery.md). Contracts:
[spec index](specs/README.md).

## Runtime

| Module | Owns |
| --- | --- |
| `GoalContract` | risk, scope, checks, stops |
| `TaskLifecycle` | durable phase, replay, recovery |
| `PlaybookPhase` | ordered phase requirements |
| `EvidenceLedger` | hash chain and artifacts |
| `HITLGate` | plan/action/completion policy |
| `HttpAdapter` | scoped transport |
| `A11yBrowserAdapter` | isolated browser primitives |
| `SkillStore` | candidate, outcome, promotion, recall |
| `ReferenceOrchestrator` | secret-free checkpoint, lease, and deterministic handoff loop |

## State

```text
planned -> approved -> executing -> verified
                   -> blocked | rejected
phase: pending -> running -> passed | failed | blocked
```

| State | Basis |
| --- | --- |
| executing | current phase + permitted action |
| passed phase | fresh evidence + two verdicts |
| blocked | stop, uncertain effect, or escalation |
| verified | phases/checks/quorum/final HITL |
| rejected | terminal human rejection |

The frozen hypothesis limit blocks effects. Phase approval cites the exact
rejection streak. Process loss resumes status; safe requests may be submitted
exactly, while uncertain effects require human resolution.

## Caller-driven external MCP sequence

```text
install -> init -> begin -> lease -> recommended_next loop -> complete -> reflect
        -> release -> outcomes -> HITL promote -> recall
```

`init` returns `active|pending|broken`; host adoption and client trust are never
guessed. Only a verified terminal task's reflection creates a local candidate;
a rejected reflection returns no skill and must not enter the outcome flow.
Outcomes cite exact terminal verification evidence. Promotion uses fixed
thresholds and stores no proof.

`recommended_next` chooses one transition; the older `allowed_next` remains the
full compatible set. Once claimed, task mutations require the active controller
lease. Leases live in a separate ledger scope and never count as task evidence.

Embedded clients may construct `ReferenceOrchestrator` with an MCP transport and
`CheckpointStore`. Call `bootstrap`, `begin`, then `advance` until it returns a
host, human, or verifier handoff. Submit that independently produced result via
`submit_host_action`, `submit_human`, or `submit_verifier`; repeat until the
terminal directive confirms reflection and lease release.

## Boundaries

Server policy is the authority ceiling; contracts only narrow it. Paths stay in
the project. Commands are argv, never shell strings. HTTP uses exact origins and
auth aliases. Browser output is untrusted. Completion, reflection, and audited
recall derive from durable evidence, not agent prose.
