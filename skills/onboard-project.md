# Skill: onboard-project

Use for `/onboard`, session start, or any request to gather current project
context before implementation.

## Contract

- Build a compact evidence-backed context packet.
- Use bounded read-only subagents when available; the main agent stays
  controller.
- Prioritize current work, implementation plan, risks, and verification.
- Close every spawned subagent before responding.
- Do not edit project files during onboarding unless separately asked.

## Flow

1. Read `AGENTS.md` and `.causality/agent-rules.md`.
2. Read `workflow/session-bootstrap.md`; if delegation is available, read
   `workflow/subagent-driven-development.md`.
3. Inspect `causality context` or the ledger tail, plus `git status --short
   --branch`.
4. Spawn up to four read-only explorers with narrow packets:
   - repo map: architecture, entry points, tests.
   - current work: git state, recent ledger/status, active goal clues.
   - plan priority: roadmap, TODOs, implementation order.
   - verification risk: test commands, CI, fragile areas.
5. While they run, inspect only high-signal files: README, manifests, tests,
   roadmap/status docs, workflow docs, and scoped memory.
6. Wait with a bounded timeout, collect reports, then close every subagent.
7. Synthesize an Onboard Packet: project snapshot, current work, priority plan,
   verification commands, risks/questions, and subagent accounting.

Treat subagent prose as a claim unless it cites files, command output, or ledger
refs. If subagent tools are unavailable, state that and perform the same
read-only inspection sequentially.
