# Implementation Specs

Specs are numbered in implementation order and belong to the delivery plans in
[the plan index](../plans/README.md).
They define public interfaces, persistence, failure behavior, and acceptance
tests. Keep each spec decision-complete and under the document budget.

| Order | Spec | Status | Depends on |
| --- | --- | --- | --- |
| 001 | [Install activation](001-install-activation.md) | implemented | — |
| 002 | [Verification evidence](002-verification-evidence.md) | implemented | 001 |
| 003 | [MCP task lifecycle](003-mcp-task-lifecycle.md) | implemented | 001, 002 |
| 004 | [API/browser adapters](004-api-browser-adapters.md) | implemented | 002, 003 |
| 005 | [Workflow and debug loop](005-workflow-debug-loop.md) | implemented | 002, 003, 004 |
| 006 | [Resume and skill operations](006-resume-and-skill-operations.md) | implemented | 003, 005 |
| 007 | [Automatic orchestration](007-automatic-orchestration.md) | implemented | 001–006 |
