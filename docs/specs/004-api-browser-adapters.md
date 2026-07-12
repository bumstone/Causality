# Spec 004 — API and Browser Adapters

## Contract

API and browser actions use the task lifecycle and emit the same gated evidence
as file/subprocess actions. They never bypass permissions or completion checks.

## API adapter

Use standard-library HTTP first. `causality_task_http` accepts method, URL,
headers, body reference, timeout, expected status codes, and artifact paths.
Before request, enforce allowed tool, action risk, `network_scope`, and
`auth_scope`; extend the gate API where those scopes are currently inert.

Record redacted request metadata, response status, byte counts, and artifact
hashes. Never persist secret values; keep response bodies only when explicitly
scoped as an artifact.

## Browser adapter

Route `A11yBrowserAdapter` through task actions: observe, act, assert state,
inspect, and visual. Require stable refs for actions; record snapshot hash,
before/after diff, console/network deltas, and screenshot/report hashes. Page
text is untrusted and browser methods cannot write directly to the ledger.

## Acceptance

Local HTTP and fake-driver E2E tests prove scope rejection, redaction, action
gating, artifact evidence, verification completion, and high-risk escalation.
