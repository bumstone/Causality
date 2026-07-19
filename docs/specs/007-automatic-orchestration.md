# Spec 007 — Automatic Orchestration

Status: active. 007A–007B implemented; 007C pending. Depends on Specs 001–006.

## Contract

An installer-owned client skill runs a restart-safe loop over existing closed
MCP tools; no one-shot effectful API is added. The host owns reasoning and edits;
Causality owns order, scope, evidence, stops, HITL, and terminal truth.

Official pre-session bootstrap is the installed Python package plus
`causality install-agent --client <name> --verify`. The experimental plugin
manifest is not a runtime contract. MCP `causality_init(verify=true)` only
converges an already loaded session; `pending|broken` stops with remediation.

## Sequence

1. Call `causality_init(client, verify=true)`. Continue only when `active`; return
   exact operator guidance for `pending|broken`.
2. Read advertised tools and context. Missing HTTP/browser capability disables
   that path; it is never guessed.
3. Begin or resume one task, acquire `causality_task_lease`, and follow the one
   persisted `recommended_next`. `allowed_next` remains the compatible choice set.
4. Per phase: start, act in scope, verify, collect two cited verdicts, then pass
   or enter hypothesis/debug/HITL handling.
5. Complete only through the server gate. Reflect every terminal task; only a
   verified reflection may create a skill candidate.
6. Return terminal result, task ID, and event hash. Skill outcome/promotion stays
   the multi-task, proof-backed Spec 006 flow.

## Recovery and safety

- Before every mutation, refresh resume state. The 007B checkpoint may contain
  only controller/lease, task/phase, operation/key, canonical request hash,
  last event hash, state, and timestamp. Raw request, proof, credentials,
  browser/HTTP content, and ledger payload stay out.
- Same key+digest may replay. A conflicting retry stops. An intent without a
  result stops for human resolution; external effects are never guessed.
- Network/auth/write scope and browser capability remain server-policy ceilings.
- Ledger records phase through reflection so failed and successful runs survive
  process restart.

## 007A wire additions

- `task.recommended_next`: `{operation, tool, reason, requires_human, ...ids}`.
- `causality_task_lease`: acquire/renew/release, 5–300 second server lease.
- Mutation inputs accept optional `controller_id` + `lease_id`. Legacy tasks stay
  compatible; after first claim every mutation requires the active lease.
- Lease events use `controller:<task_id>` scope, outside task evidence/provenance.
  A lease coordinates writers and is not an authentication proof.

## 007B reference driver

- `ReferenceOrchestrator` provides begin, claim, bounded deterministic advance,
  host-action, HITL, verifier, completion, reflection, and lease-release paths.
- The checkpoint is a closed, controller-namespaced JSON document. It stores
  only controller/lease/task/phase IDs, operation/idempotency key, request and
  event hashes, status, and timestamp. It never stores a raw request or proof.
- A prepared key+digest is replayable. A conflict stops. Proof-bearing response
  loss is not automatically resent; resume may only clear it when durable task
  state proves the server already advanced.
- Automatic verdicts carry a host-asserted `provider_id`. Two verifier names
  from the same provider do not satisfy the orchestrated quorum. This is an
  auditable independence claim, not cryptographic provider attestation.
- Lease changes record a whitelist-only environment snapshot in
  `controller:<task_id>`: package/Python/OS, advertised capability names and
  digest, policy digest, and Git HEAD/dirty state. Policy values, credentials,
  request content, command output, host/user names, and paths are excluded.

## Acceptance

A fresh external project runs install → active loop → success and fail/debug
paths across forced restarts at every mutating boundary. Tests prove exact-once
effects, current evidence, two-verifier quorum, HITL stops, capability omission,
secret non-disclosure, chain validity, terminal replay, and rejected-reflection
skill absence on Windows and Python 3.11–3.13.
