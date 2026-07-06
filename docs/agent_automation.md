# Agent Automation

`causality install-agent` makes a new project behave like it has a local
agent plugin installed.

## What it installs

```text
AGENTS.md
CLAUDE.md
.claude/commands/onboard.md
.claude/commands/causality-plan.md
.claude/commands/causality-verify.md
.claude/commands/causality-root-cause.md
.claude/commands/causality-a11y-observe.md
.claude/commands/causality-complete.md
.codex/causality-routing.md
.causality/agent-rules.md
.causality/ledger.jsonl
.causality/causality-workflows.json
.causality/mcp.json
skills/onboard-project.md
```

`AGENTS.md` and `CLAUDE.md` point the agent to `.causality/agent-rules.md`.
The rule file keeps the HITL loop stable across sessions:

```text
Goal contract -> gate -> evidence ledger -> verifier passes -> completion gate
```

## Slash commands and automatic routing

Claude project slash commands:

- `/onboard`
- `/causality-plan`
- `/causality-verify`
- `/causality-root-cause`
- `/causality-a11y-observe`
- `/causality-complete`

Codex uses `AGENTS.md` and `.codex/causality-routing.md` as routing context.
For onboarding, read `skills/onboard-project.md`; for planning, debugging,
browser/UI testing, implementation verification, or completion, choose the
matching workflow without requiring the user to name it.

## MCP-style server

Start the server:

```powershell
python -m causality.mcp_server --project .
```

Tools exposed:

- `causality_init`: install/update the project automation files.
- `causality_context`: return ledger tail and workflow names.
- `causality_append_evidence`: append evidence to `.causality/ledger.jsonl`.
- `causality_workflows`: return the workflow manifest.

The implementation is intentionally local and dependency-light. It stores all
state in the project, so it can be used by Claude, Codex, or any client that can
read project instruction files or register a stdio tool server.
