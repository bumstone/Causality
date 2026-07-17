# Agent Automation

`causality install-agent` installs local routing and audit support:

Planned installation changes are tracked in [Plan 001](plans/001-external-harness-delivery.md);
this document describes current behavior.

```text
AGENTS.md, CLAUDE.md
.claude/commands/{onboard,causality-*}.md
.codex/{causality-routing.md,config.toml}, .mcp.json
.causality/{.gitignore,agent-rules.md,ledger.jsonl,causality-workflows.json,mcp.json,install-report.json}
workflow/, checklists/, skills/, memory/
```

`AGENTS.md` and `CLAUDE.md` are host-owned after initial creation. Forced
refreshes preserve them while updating namespaced commands and generated
workflow/checklist/skill files.

Use `--client codex|claude|generic --verify`; `--adopt` appends only a managed
routing pointer. Results are `active`, `pending` (trust/approval/adoption), or
`broken` (invalid config or failed handshake). MCP `causality_init` exposes only
safe `client` and `verify` options; force/adoption require the operator CLI.

Native MCP entries pin the local interpreter and use an isolated absolute launcher.
The installed ignore keeps untracked runtime state private; pretracked private
paths fail with cleanup guidance. This repository ignores `.codex/config.toml`
and `.mcp.json`; hosts retain their own config policy and unrelated entries.

The installed control loop is:

```text
Goal contract -> gate -> evidence ledger -> verifier passes -> completion gate
```

Claude exposes `/onboard` and the `causality-*` slash commands. Codex routes
through `AGENTS.md` and `.codex/causality-routing.md`; detailed instructions are
loaded from the matching on-demand workflow or skill.

## MCP server

```powershell
python -I -m causality.mcp_server --project .
```

It exposes closed task lifecycle tools, scoped HTTP, context, and workflow
metadata. The browser tool appears only when an explicit protocol-v1 wrapper
passes capability checks; there is no `PATH` discovery. State remains local and
the runtime has no third-party Python dependencies. HTTP/browser network effects
remain disabled until server policy names exact origins and required approvals.
