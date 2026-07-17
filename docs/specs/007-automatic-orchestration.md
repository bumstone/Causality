# Spec 007 — Automatic Orchestration

Status: planned. Depends on Specs 001–006.

## Contract

An installer-owned client skill runs a restart-safe loop over existing closed
MCP tools; no one-shot effectful API is added. The host owns reasoning and edits;
Causality owns order, scope, evidence, stops, HITL, and terminal truth.

## Sequence

1. Call `causality_init(client, verify=true)`. Continue only when `active`; return
   exact operator guidance for `pending|broken`.
2. Read advertised tools and context. Missing HTTP/browser capability disables
   that path; it is never guessed.
3. Begin or resume one task. Follow persisted `allowed_next`; create/approve the
   contract before effects.
4. Per phase: start, act in scope, verify, collect two cited verdicts, then pass
   or enter hypothesis/debug/HITL handling.
5. Complete only through the server gate. Reflect every terminal task; only a
   verified reflection may create a skill candidate.
6. Return terminal result, task ID, and event hash. Skill outcome/promotion stays
   the multi-task, proof-backed Spec 006 flow.

## Recovery and safety

- Before every mutation, refresh resume state. Persist only task ID and last
  response hash; secrets, proof, browser text, and raw ledger payload stay out.
- Same key+digest may replay. A conflicting retry stops. An intent without a
  result stops for human resolution; external effects are never guessed.
- Network/auth/write scope and browser capability remain server-policy ceilings.
- Ledger records phase through reflection so failed and successful runs survive
  process restart.

## Acceptance

A fresh external project runs install → active loop → success and fail/debug
paths across forced restarts at every mutating boundary. Tests prove exact-once
effects, current evidence, two-verifier quorum, HITL stops, capability omission,
secret non-disclosure, chain validity, terminal replay, and rejected-reflection
skill absence on Windows and Python 3.11–3.13.
