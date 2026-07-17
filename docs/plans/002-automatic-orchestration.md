# Plan 002 — Automatic Orchestration

Status: planned.

## Goal

Let an installed client skill drive the existing MCP lifecycle from activation
to durable completion. The host agent still plans, reasons, and edits; Causality
orders phases, gates effects, records evidence, and decides completion.

## Ordered PRs

1. [ ] **007A — protocol and routing**
   - Add an installer-owned `causality-orchestrate` skill and route.
   - Start with `causality_init(verify=true)`; stop on `pending|broken`.
   - Derive every next call from `tools/list`, context, and task resume state.
2. [ ] **007B — durable loop and recovery**
   - Drive phase → action → verify → debug/retry/HITL → complete → reflect.
   - Persist a client checkpoint containing only task ID and last response hash.
   - Restart after each mutating boundary; never replay an uncertain effect.
3. [ ] **007C — external acceptance**
   - Run fresh-project success, verification failure, recovery, and rejection E2E.
   - Assert exact-once terminal effects, ledger chain, failure/success history,
     capability gates, secret redaction, and no skill after rejected reflection.

## Release gates

Each PR stays below the review budget and needs code/security review, Windows
concurrency stress, Python 3.11–3.13 CI, two evidence-citing verifiers, and the
Causality completion gate. 007C alone may mark Spec 007 implemented.
