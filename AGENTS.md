# Codex Execution Rules

This is the Codex execution entry point. Follow `.causality/agent-rules.md` for
Causality planning, evidence, and completion gates. Before changing code
for a non-trivial task, inspect the current ledger context with:

```powershell
causality context
```

Do not complete high-risk work without ledger evidence, verifier passes, and
human approval when required.

## Context Economy

Keep always-loaded context minimal (ADR 0007). Do not paste long workflows,
checklists, or role descriptions here. Read the on-demand files only when the
task requires them: `workflow/<type>.md`, `checklists/<type>.md`,
`skills/<name>.md`, and scoped `memory/<type>/` entries.

## Intent Routing

Once the task type is fixed, read only the matching workflow document:

- Planning request: `workflow/writing-plans.md` (the `causality-plan` flow).
- Implementation request: plan gate, evidence ledger, and verifier checks.
- Debugging request: `workflow/root-cause-protocol.md`.
- Browser/UI request: the `causality-a11y-observe` flow when a driver is configured.
- Completion request: `workflow/verification-before-completion.md` before claiming done.
