# Agent Automation

`causality install-agent` installs project-local routing and audit support. See
[Plan 001](plans/001-external-harness-delivery.md) for delivery status.

```text
AGENTS.md, CLAUDE.md
.claude/commands/{onboard,causality-*}.md
.codex/{causality-routing.md,config.toml}, .mcp.json
.causality/{.gitignore,agent-rules.md,ledger.jsonl,causality-workflows.json,mcp.json,install-report.json}
workflow/, checklists/, skills/, memory/
```

`AGENTS.md` and `CLAUDE.md` are host-owned after creation. `--force` preserves
them and refreshes namespaced commands/generated assets. Use
`--client codex|claude|generic --verify`; `--adopt` appends one managed routing
pointer. Results are `active`, `pending` (trust/approval/adoption), or `broken`.
MCP `causality_init` exposes only safe `client` and `verify` options;
force/adoption require the operator CLI.

Native MCP entries pin the local interpreter and use an isolated absolute
launcher. Runtime state remains ignored; pretracked private paths fail with
cleanup guidance. Host repositories retain their own MCP config policy.

```text
contract -> persisted phase -> action/evidence -> two verdicts
         -> phase finish/retry/HITL -> completion -> reflection
```

The host agent reasons and edits. Causality owns contract, phase order, scopes,
stop policy, durable evidence, and completion.

## MCP server

```powershell
python -I -m causality.mcp_server --project .
```

MCP `init` installs and reports `active|pending|broken`; host adoption/trust is
never guessed. `begin` selects `auto` workflow. Closed tools expose phases,
actions, verify/verdict/complete/reflect, resume/context, and scoped HTTP.
Browser operations appear only after an explicit protocol-v1 capability check;
the default server does not advertise the browser tool.
Verified reflection creates a local skill candidate. Three task-bound outcomes
with at least two successes can reach proof-backed promotion; recall returns
authored skills first and only audited promoted skills. Reaching the frozen
hypothesis limit returns exact approval evidence. Uncertain effects require
human resolution and are never replayed automatically.
