# Installation

## Package or checkout

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
# --client claude or generic
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

Clients use native config; trust and approval remain user gates. Manual start:

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

Task subprocesses never inherit `CAUSALITY_*`. Browser support is also
default-deny and requires an explicit wrapper command:

```powershell
$env:CAUSALITY_BROWSER_COMMAND_JSON='["python","C:\\tools\\browser_wrapper.py"]'
# legacy single executable:
$env:CAUSALITY_BROWSER_BIN='C:\tools\causality-browser.exe'
```

There is no `PATH` discovery. Protocol v1 must advertise isolated sessions,
network-scope enforcement, and observe/act/assert/inspect/visual. Causality
supplies private task session/profile paths and exact origins; the wrapper
enforces them. The package does not bundle/launch a browser, navigate URLs,
reuse personal profiles, or inject browser credentials.
