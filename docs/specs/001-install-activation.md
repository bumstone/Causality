# Spec 001 — Install Activation

Status: implemented.

## Contract

`install-agent` installs assets and reports server/client readiness. `--force`
preserves root `AGENTS.md`/`CLAUDE.md`; trust/approval stays human-owned.

## Interface

- `--client auto|codex|claude|generic` (default `auto`) and `--verify`.
- `auto` resolves one client signal; zero/many is `pending` with a rerun command.
- `--adopt` adds an idempotent marker block to the selected host entry
  file. Without it, preserve files and return `activation: pending` plus the
  exact snippet required to activate routing.
- Pin MCP to `sys.executable` with an isolated launcher. Merge into Codex
  `.codex/config.toml` or Claude `.mcp.json`; preserve unrelated settings.
- `--verify` starts the configured server, sends `initialize` and `tools/list`,
  then probes client loading when supported.
- MCP init accepts `client`/`verify`; force and host adoption remain explicit
  CLI-only operator actions.

## State

Precedence: `broken > pending > active`. Bad config/handshake is `broken`;
missing adoption, verification, trust, or approval is `pending`. `active` means
all applicable checks passed; generic mode has no trust probe.

## Persistence and failure

Durably write `.causality/install-report.json` with client, files, interpreter,
handshake/probe, remediation, and timestamp.
Only `broken` exits nonzero; generated files remain for diagnosis.
Write `.causality/.gitignore` before runtime state. Pretracked private paths fail
`broken` with untrack guidance.

## Acceptance

- Verified generic and trusted/approved Codex/Claude fixtures report `active`;
  unresolved trust/approval reports `pending`.
- Existing host files stay byte-identical without `--adopt`; marker insertion is
  idempotent with it.
- A venv-only package is found by the generated MCP command.
- Malformed config, partial markers, and broken interpreters preserve user files
  and produce actionable `broken` output.
