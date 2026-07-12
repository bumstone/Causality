# Plan 001 — External Harness Delivery

## Goal

Make an installed Causality harness executable and auditable from an external
project through its MCP server. A host agent still performs reasoning and code
edits; the runtime owns contracts, gated actions, evidence, verification, and
terminal state.

## Ordered delivery

1. [x] [Install activation](../specs/001-install-activation.md): safe client setup,
   interpreter pinning, and an explicit active/pending result.
2. [ ] [Verification evidence](../specs/002-verification-evidence.md): execute
   declared checks and bind completion to requirement IDs, exit codes, and hashes.
3. [ ] [MCP task lifecycle](../specs/003-mcp-task-lifecycle.md): create, approve,
   act, verify, complete, and reflect one persistent task.
4. [ ] [API/browser adapters](../specs/004-api-browser-adapters.md): route API and
   A11y actions through the same task gates and evidence model.
5. [ ] [Workflow and debug loop](../specs/005-workflow-debug-loop.md): persist phase
   state, route debugging, count failed hypotheses, and deduplicate failures.
6. [ ] [Resume and skill operations](../specs/006-resume-and-skill-operations.md):
   resume interrupted tasks and expose earned-skill outcome/promotion operations.

## Boundaries

- No arbitrary shell string execution; command actions use argv lists and frozen
  permissions.
- No silent rewrite of host `AGENTS.md` or `CLAUDE.md`; adoption is explicit.
- A phase is released only after unit, E2E, and external-project fixture tests pass.
