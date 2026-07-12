# Spec 002 — Verification Evidence

Status: implemented (2026-07-11).

## Contract

Completion requires each required ID's latest pass and fresh generic
evidence. Two independent verdicts—and high-risk final approval—each cite the
current hashes. Risk is canonical; high-risk plan approval stays active.

## Model

Requirements store ID, argv, exits, timeout, required/manual, and
`{artifact_path: sha256|null}` under the durable workspace root. Manual checks
declare no argv/artifacts; they need same-task evidence, a boolean verdict, and
a named human.

Legacy strings remain one minor: parse to argv/`verify-NNN` and warn. Legacy
keeps durable binding and a two-verifier floor.

## Execution

`verify_requirement(contract, id, root)` gates argv with `shell=False` and
disables bytecode writes. Evidence records argv, exits, UTF-8 byte counts,
artifact state, workspace digest, status, reason, and time.
`VerificationResult.event_hash` equals the ledger event's `entry_hash`.

Failures are recorded. Artifacts stay regular in-root files with the
recorded resolved path, mode, and hash.

## Freshness and durability

Completion rechecks project, dependency, bytecode, symlink-target, and full Git
state. Mutations stale prior checks. Latest requirement result wins; verifier
IDs cannot repeat per batch. Invalid citations fail.

Contract creation, public gated actions, verification, and completion serialize
on one durable lock. The hash chain plus tail anchor detects edits,
rotation gaps, and current-segment deletion.

## Boundary and acceptance

The entire `.causality/` tree and analysis caches (`.pytest_cache`, `.mypy_cache`,
`.ruff_cache`) are excluded. Outside-root/remote effects, declared argv, and raw
ledger writers are trusted; this is not an OS sandbox or writer-authentication
system; hostile commands need an external sandbox.

Spec 003 adds task/MCP lifecycle binding. Unit, concurrency, rotation, engine
E2E, and pip-installed external-project fixtures cover this phase.
