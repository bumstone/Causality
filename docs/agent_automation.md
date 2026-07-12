# Agent Automation

`causality install-agent` installs local routing and audit support:

Planned installation changes are tracked in [Plan 001](plans/001-external-harness-delivery.md);
this document describes current behavior.

```text
AGENTS.md, CLAUDE.md
.claude/commands/{onboard,causality-*}.md
.codex/{causality-routing.md,config.toml}, .mcp.json
.causality/{agent-rules.md,ledger.jsonl,causality-workflows.json,mcp.json,install-report.json}
workflow/, checklists/, skills/, memory/
```

`AGENTS.md` and `CLAUDE.md` are host-owned after initial creation. Forced
refreshes preserve them while updating namespaced commands and generated
workflow/checklist/skill files.

Use `--client codex|claude|generic --verify`; `--adopt` appends only a managed
routing pointer. Results are `active`, `pending` (trust/approval/adoption), or
`broken` (invalid config or failed handshake). `causality_init` exposes the same
options over MCP.

Native MCP entries pin the local interpreter. This repository ignores
`.codex/config.toml` and `.mcp.json`; a host with an existing shared config keeps
its own tracking policy and unrelated entries.

The installed control loop is:

```text
Goal contract -> gate -> evidence ledger -> verifier passes -> completion gate
```

Claude exposes `/onboard` and the `causality-*` slash commands. Codex routes
through `AGENTS.md` and `.codex/causality-routing.md`; detailed instructions are
loaded from the matching on-demand workflow or skill.

## MCP server

```powershell
python -m causality.mcp_server --project .
```

It exposes initialization, context, evidence append, and workflow-manifest
tools. State remains local to the project and the runtime has no third-party
Python dependencies.
