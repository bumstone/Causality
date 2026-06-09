# Agent Automation

`ouroboros-hitl install-agent` makes a new project behave like it has a local
agent plugin installed.

## What it installs

```text
AGENTS.md
CLAUDE.md
.claude/commands/ouroboros-plan.md
.claude/commands/ouroboros-verify.md
.claude/commands/ouroboros-root-cause.md
.claude/commands/ouroboros-a11y-observe.md
.claude/commands/ouroboros-complete.md
.codex/ouroboros-routing.md
.ouroboros/agent-rules.md
.ouroboros/ledger.jsonl
.ouroboros/ouroboros-workflows.json
.ouroboros/mcp.json
```

`AGENTS.md` and `CLAUDE.md` point the agent to `.ouroboros/agent-rules.md`.
The rule file keeps the HITL loop stable across sessions:

```text
Goal contract -> gate -> evidence ledger -> verifier passes -> completion gate
```

## Slash commands and automatic routing

Claude project slash commands:

- `/ouroboros-plan`
- `/ouroboros-verify`
- `/ouroboros-root-cause`
- `/ouroboros-a11y-observe`
- `/ouroboros-complete`

Codex uses `AGENTS.md` and `.codex/ouroboros-routing.md` as routing context.
When the user asks for planning, debugging, browser/UI testing, implementation
verification, or completion, the agent should choose the matching workflow
without requiring the user to name it.

## MCP-style server

Start the server:

```powershell
python -m ouroboros_hitl.mcp_server --project .
```

Tools exposed:

- `ouroboros_init`: install/update the project automation files.
- `ouroboros_context`: return ledger tail and workflow names.
- `ouroboros_append_evidence`: append evidence to `.ouroboros/ledger.jsonl`.
- `ouroboros_workflows`: return the workflow manifest.

The implementation is intentionally local and dependency-light. It stores all
state in the project, so it can be used by Claude, Codex, or any client that can
read project instruction files or register a stdio tool server.
