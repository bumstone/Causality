# Installation

## Package or local checkout

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

Helpers install the venv, package, agent files, and doctor checks:

```powershell
# Windows
.\scripts\install.ps1

# Update; options: -SkipTests
.\scripts\update.ps1
```

```bash
# Linux/WSL
bash scripts/install.sh
bash scripts/update.sh           # option: --skip-tests
```

Run `scripts/doctor.ps1` or `scripts/doctor.sh` for a health check.

## Project agent files

From a target project:

```powershell
causality install-agent --client codex --adopt --verify
# or: --client claude / --client generic
```

Installs host entrypoints, namespaced routing, local rules/ledger/MCP config,
and on-demand workflow, checklist, skill, and memory files.

Host `AGENTS.md` and `CLAUDE.md` are never overwritten. `--force` refreshes
other generated files; update helpers use it automatically for schema changes.
MCP `causality_init` accepts only `client` and `verify`; `--force` and `--adopt`
are CLI-only operator actions.

`auto` needs exactly one existing Codex/Claude signal; otherwise it returns an
explicit rerun command.
`active` means routing, config, handshake, and applicable client probes passed;
`pending` needs adoption/trust/approval; `broken` is a real config/runtime error.

## MCP and browser adapters

Codex uses `.codex/config.toml`, Claude root `.mcp.json`, and generic clients
`.causality/mcp.json`. Codex trust and Claude approval remain user gates.
Generated native entries contain machine paths; keep them local unless the host
uses a portable shared command. Manual stdio start:

```powershell
python -I -m causality.mcp_server --project .
```

`.causality/.gitignore` hides raw runtime state. If a legacy private path is
already tracked, install returns `broken` with untrack guidance.

Set the browser driver executable when browser actions are needed:

```powershell
$env:CAUSALITY_BROWSER_BIN="C:\path\to\browser-driver.exe"
```
