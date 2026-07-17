# Spec 003 — MCP Task Lifecycle

Status: implemented.

## Wire

Inputs are closed JSON objects. Invalid fields return `validation_error` before
write. Success text: `{ok,task,event_hash,idempotency,data}`. Failure sets
`isError`: `{ok:false,error:{code,message,retryable,details},task?}`.

Exact fields, conditional requirements, result envelope, state mapping, and
error triggers are normative in [MCP Wire Contract](003-mcp-wire.md) and the
server's closed `tools/list.inputSchema`.

## Invariants

Authority = server policy ∩ frozen contract. Paths/cwd stay in project; empty
write scope denies writes. Subprocess needs an argv allowlist, not an OS
sandbox. Ledger is sole truth; all segments fold under one lock.

Edges: `planned→approved|executing|rejected`,
`approved→executing|blocked|rejected`, `executing→verified|blocked|rejected`,
`blocked→executing|rejected`. Terminal states never reopen; no raw transition.

Effect order: reload→gate→intent→effect→result. Orphans block without replay;
only trusted `not_applied` reopens; its first durable decision reserves the
effect. Durable rejection and partial ESCALATE/STOP block effects immediately.
PASS recovery binds both ledger position and workspace fingerprint; drift makes
the old key stale. Reflection uses a terminal intent, deterministic append-once
memory IDs, and one manifest.

## Acceptance

Crash tests plus installed-wheel stdio E2E prove restart, exact-once effects
and ledger validity.
