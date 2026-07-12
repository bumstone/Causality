# Spec 004 — API and Browser Adapters

## Delivery

- [x] **004A HTTP:** exact scopes, approval, redacted evidence, artifact,
  closed MCP tool, restart E2E.
- [x] **004B browser:** capability-gated wrapper, isolated sessions, stable refs,
  state-bound actions, untrusted replay, and A11y/diff evidence.

## Contract

HTTP/browser effects use the persistent lifecycle and cannot bypass permissions,
approval, evidence, verification, or completion.

## HTTP

`causality_task_http` enforces tool/risk, exact network/auth scopes, public
headers, and credential aliases. It records redacted metadata, status, byte
counts, and explicit artifact hashes. Redirects and task-process credential
inheritance are forbidden.

## Browser

`A11yBrowserAdapter` speaks wrapper protocol v1 over bounded JSON subprocess
calls. MCP enables it only through `CAUSALITY_BROWSER_COMMAND_JSON` or the legacy
single binary setting. Handshake must prove isolated sessions, network-scope
enforcement, and observe/act/assert/inspect/visual capabilities.

Each task gets a private session/profile. Observe returns stable refs and a
canonical state hash. Later operations reject stale task state; effects recheck
current origins, and every act requires `external_send` approval.

Raw driver data stays in a hash-verified ignored cache and MCP returns it only as
untrusted data. The ledger stores intent, state/snapshot hashes, diff and
diagnostic hashes, and artifact refs. The adapter never writes the ledger.

Browser launch/navigation, personal profiles, credential injection,
selectors/JavaScript, and driver-specific compatibility remain host-wrapper
responsibilities.

## Acceptance

Installed-project HTTP and fake-wrapper browser E2E prove scope rejection,
isolation, redaction, state/action gates, restart replay, artifact evidence,
verification, and verifier quorum.
