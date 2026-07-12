# Installation

## Package or local checkout

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

Helpers install the package, agent files, and checks:

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

Installs routing, local ledger/MCP config, and on-demand workflow/skill files.

Host `AGENTS.md` and `CLAUDE.md` are never overwritten. `--force` refreshes
other generated files; update helpers use it automatically for schema changes.
MCP `causality_init` accepts only `client` and `verify`; `--force` and `--adopt`
are CLI-only operator actions.

`auto` needs exactly one existing Codex/Claude signal; otherwise it returns an
explicit rerun command.
`active` passed probes; `pending` needs adoption/trust/approval; `broken` failed.

## MCP and adapters

Codex uses `.codex/config.toml`, Claude `.mcp.json`, and generic clients
`.causality/mcp.json`. Trust and approval remain user gates. Manual start:

```powershell
python -I -m causality.mcp_server --project .
```

`.causality/.gitignore` hides raw runtime state. If a legacy private path is
already tracked, install returns `broken` with untrack guidance.

HTTP is default-deny. Set exact origins, aliases, allowed public headers, and
approval proof in the MCP environment:

```powershell
$env:CAUSALITY_NETWORK_ORIGINS_JSON='["https://api.example.com"]'
$env:CAUSALITY_AUTH_REFS_JSON='["service-token"]'
$env:CAUSALITY_HTTP_HEADERS_JSON='["Content-Type"]'
$env:CAUSALITY_HTTP_CREDENTIALS_JSON='{"service-token":{"Authorization":"Bearer ..."}}'
$env:CAUSALITY_APPROVAL_TOKEN='operator-secret'
```

Secrets stay in MCP only; task subprocesses do not inherit `CAUSALITY_*`.
Browser lifecycle remains Spec 004B; primitives are not a completion claim.
