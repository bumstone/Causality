# Spec 001 — Install Activation

Status: implemented (2026-07-11).

## Contract

`causality install-agent` installs generated assets and truthfully reports
server and client readiness. `--force` never overwrites root `AGENTS.md` or
`CLAUDE.md`; client trust/approval is never changed.

## Interface

- `--client auto|codex|claude|generic` (default `auto`) and `--verify`.
- `auto` resolves exactly one existing client signal; zero or many is `pending`
  with an explicit-client command.
- `--adopt` adds an idempotent marker block to the selected host entry
  file. Without it, preserve files and return `activation: pending` plus the
  exact snippet required to activate routing.
- Pin MCP commands to `sys.executable`. Merge the namespaced entry into Codex
  `.codex/config.toml` or Claude `.mcp.json`; preserve unrelated settings.
- `--verify` starts the configured server, sends `initialize` and `tools/list`,
  then probes client loading when supported.

## State

Precedence is `broken > pending > active`. Invalid/conflicting config or failed
handshake is `broken`. Missing adoption, verification, Codex trust, or Claude
approval is `pending`. `active` means all applicable checks passed; generic mode
has no client trust probe.

## Persistence and failure

Durably write `.causality/install-report.json` with requested/resolved client,
files, interpreter, handshake, client probe, remediation, and UTC timestamp.
Only `broken` exits nonzero; generated files remain for diagnosis.

## Acceptance

- Verified generic and trusted/approved Codex/Claude fixtures report `active`;
  unresolved trust/approval reports `pending`.
- Existing host files stay byte-identical without `--adopt`; marker insertion is
  idempotent with it.
- A venv-only package is found by the generated MCP command.
- Malformed config, partial markers, and broken interpreters preserve user files
  and produce actionable `broken` output.
