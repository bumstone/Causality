# Causality Integration Structure

## Runtime modules

- `GoalContract`: stores risk, permissions, evidence requirements, state, and stopping policy.
- `EvidenceLedger`: append-only JSONL with hash chaining and artifact hashing.
- `HITLGate`: blocks high-risk plans, irreversible actions, missing evidence, verifier conflicts, and final approval gaps.
- `A11yBrowserAdapter`: exposes `observe`, `act`, `assert_state`, `inspect`, and `visual`.
- `WorkflowTemplate`: Causality workflow contracts for planning, subagents, verification, TDD, root-cause checks, and bootstrap context.

## State transition policy

```text
planned -> approved -> executing -> verified
                   \-> blocked
                   \-> rejected
```

Allowed transition evidence:

- `planned`: goal contract exists.
- `approved`: plan gate passed or human approval exists.
- `executing`: tool broker approved the action.
- `verified`: required evidence exists, two verifier passes exist, final approval exists when required.
- `blocked`: no progress, failed hypotheses, missing context, or human escalation.
- `rejected`: explicit human rejection or critical unresolved policy failure.

## HITL placement

Human approval is required for:

- high-risk plans
- irreversible contracts
- delete, deploy, payment, external send, or permission-change actions
- critical verifier disagreement
- evidence insufficiency waiver
- final acceptance for high-risk work

Human approval must include stage, approver, rationale, and raw artifact references.

## A11y compression design

The browser daemon keeps DOM, cookies, tabs, storage, ref maps, and prior snapshots.
The LLM receives only:

- URL/title/viewport summary when needed
- compact or interactive accessibility tree
- stable refs such as `@e12`
- diff after an action
- console/network deltas
- artifact paths for screenshots and reports

The adapter always treats page output as untrusted external content. Page text never
becomes an instruction source.

## Escalation ladder

```text
compact A11y snapshot
-> scoped A11y subtree
-> forms/attrs/html inspection
-> annotated screenshot
-> human review
```

Use the cheapest observation that resolves the next action decision.
