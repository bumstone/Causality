# Installation

## Python package

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

For Q00/Ouroboros itself:

```powershell
pip install ouroboros-ai
```

## Project-level agent automation

In a new project:

```powershell
causality install-agent
```

This installs local instruction/config files:

- `AGENTS.md`
- `CLAUDE.md`
- `.claude/commands/causality-plan.md`
- `.claude/commands/causality-verify.md`
- `.claude/commands/causality-root-cause.md`
- `.claude/commands/causality-a11y-observe.md`
- `.claude/commands/causality-complete.md`
- `.codex/causality-routing.md`
- `.causality/agent-rules.md`
- `.causality/ledger.jsonl`
- `.causality/causality-workflows.json`
- `.causality/mcp.json`

Existing files are not overwritten unless you pass `--force`.

Claude uses the `.claude/commands/` files as project slash commands. Codex uses
`AGENTS.md` as the automatic router and can call the MCP-style server when the
client exposes it.

For MCP-style clients, use the project config in `.causality/mcp.json` or
register the server manually:

```powershell
python -m causality.mcp_server --project .
```

## Browser driver

The browser adapter is driver-agnostic. Configure the executable with:

```powershell
$env:CAUSALITY_BROWSER_BIN="C:\path\to\browser-driver.exe"
```

The driver should expose snapshot/action commands or be wrapped by a small
adapter script that does.

## Browser accessibility tools

For downstream web projects:

```powershell
npm install -D @playwright/test @axe-core/playwright
npx playwright install
```

Optional:

```powershell
npm install -D pa11y lighthouse
```

These tools should produce JSON/HTML artifacts that are referenced from the
Causality ledger by path and hash, not pasted into prompts.
