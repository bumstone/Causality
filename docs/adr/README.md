# ADR Index

Architecture Decision Records are decision history. They explain why the project
is shaped the way it is; they are not the live implementation dashboard.

For current state, read:

- `docs/project-summary.md`
- `docs/status/roadmap.html`

## Reading Order

1. [0001 — Task Contract](0001-task-contract-as-binding-rules.md)
2. [0002 — Three-layer execution control](0002-three-layer-control-stack.md)
3. [0003 — Contract Harness](0003-contract-harness.md)
4. [0004 — Agent Harness task routing](0004-agent-harness-task-routing.md)
5. [0005 — Identity, memory, and skill substrate](0005-identity-memory-skill-substrate.md)
6. [0006 — Final blended five-layer architecture](0006-final-blended-architecture.md)
7. [0007 — Context economy and progressive disclosure](0007-context-economy-progressive-disclosure.md)
8. [0008 — Repository hygiene](0008-repository-hygiene-shared-vs-ignored.md)
9. [0009 — Review change budget](0009-review-change-budget.md)
10. [0010 — Generated document budget](0010-caveman-doc-budget.md)
11. [0011 — Ledger persistence, indexing, and durability](0011-ledger-persistence-indexing-durability.md)

## Quick Map

| Topic | ADRs |
| --- | --- |
| Goal and task contract model | 0001, 0003 |
| Execution gates and loop control | 0002, 0006 |
| Routing and context economy | 0004, 0007 |
| Memory and skill feedback | 0005, 0006 |
| Repository/process hygiene | 0008, 0009, 0010 |
| Ledger durability and scaling | 0011 |

## Maintenance Rule

Keep live implementation status out of this index. If a status claim changes,
update `docs/project-summary.md` and `docs/status/roadmap.html` instead of
duplicating a second dashboard here.
