# Spec 002 — Verification Evidence

Status: implemented (2026-07-11).

## Contract

Completion needs every required ID's latest pass, fresh generic evidence, and
two verdicts citing all current hashes. High-risk work also needs plan and final
approval.

## Model

Requirements freeze ID, argv, exits, timeout, required/manual, and artifact
hashes under the workspace root. Manual checks need same-task evidence, a
boolean verdict, and a named human.

Legacy strings parse to argv/`verify-NNN`, warn for one minor, and retain
durable binding plus the two-verifier floor.

## Execution

`verify_requirement(contract, id, root)` gates argv with `shell=False`, disables
bytecode writes, and records argv, exits, output, artifacts, workspace digest,
status, reason, and time. Each stream keeps 64 KiB plus its full UTF-8 byte
count, SHA-256, and truncation flag.
`VerificationResult.event_hash` equals the ledger event's `entry_hash`.

Artifacts remain regular in-root files with recorded path, mode, and hash.

## Freshness and durability

Completion rechecks project, dependency, bytecode, symlink-target, and Git
state. Mutations stale prior checks; latest results win. Duplicate verifier IDs
or invalid citations fail.

One durable lock serializes contract creation, actions, verification, and
completion. Hash chain and tail anchor detect edits, rotation gaps, and segment
deletion.

## Boundary and acceptance

`.causality/` and analysis caches are excluded. Outside-root effects, declared
argv, and raw ledger writers are trusted; hostile commands need an external
sandbox. The ignored ledger may contain output. Distinct verifier names are
caller-attested; the runtime validates citations, not organizational provenance.

Spec 003 adds task/MCP lifecycle binding. Unit, concurrency, rotation, engine,
and installed-project E2E tests cover this phase.
