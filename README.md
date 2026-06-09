# Ouroboros HITL

Ouroboros HITL is a local-first agent workflow kit for Claude, Codex, and
Ouroboros-style projects. It combines three ideas:

- Ouroboros-style goal contracts, ledgers, state transitions, plugin contracts, and gates
- Superpowers-style planning, TDD, root-cause debugging, verification, and slash-command ergonomics
- gstack-style browser discipline: compact A11y snapshots, stable refs, action diffs, and evidence-first QA

The implementation is standalone and dependency-light. It does not vendor
upstream project code. The upstream projects are MIT-licensed and credited in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Can This Go In A Private GitHub Repository?

Yes, this repository is suitable for a private GitHub repository.

Reasons:

- This repo is an original implementation, not a vendored copy of upstream source.
- The referenced upstream projects are MIT-licensed.
- MIT permits private use, modification, redistribution, and sublicensing when notices are preserved.
- This repo includes its own [MIT LICENSE](LICENSE) and third-party attribution in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Practical requirement: keep `LICENSE` and `THIRD_PARTY_NOTICES.md` in the
private repository. If you later copy substantial upstream source into this
project, add that upstream copyright notice too.

This is an engineering license review, not legal advice.

## What Gets Installed Into A New Project?

Run this once in a new project:

```powershell
pip install -e D:\Documents\Playground
ouroboros-hitl install-agent
```

It writes:

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

Existing files are skipped by default. Use `--force` only when you intend to
replace existing project instructions.

## Claude And Codex Usage

Claude can use project slash commands:

```text
/ouroboros-plan
/ouroboros-verify
/ouroboros-root-cause
/ouroboros-a11y-observe
/ouroboros-complete
```

Codex uses `AGENTS.md`, `.codex/ouroboros-routing.md`, and
`.ouroboros/agent-rules.md` as automatic routing context. The intended behavior
is:

- planning/spec request -> `ouroboros-plan`
- implementation or verification request -> `ouroboros-verify`
- bug/regression request -> `ouroboros-root-cause`
- browser/UI flow request -> `ouroboros-a11y-observe`
- "done", "ship", or final handoff request -> `ouroboros-complete`

## MCP-Style Tool Server

For clients that support project MCP configuration, register the stdio server:

```powershell
python -m ouroboros_hitl.mcp_server --project .
```

The generated `.ouroboros/mcp.json` contains the same command. Exposed tools:

- `ouroboros_init`: install project-level agent automation files
- `ouroboros_context`: return ledger tail and workflow names
- `ouroboros_append_evidence`: append evidence to `.ouroboros/ledger.jsonl`
- `ouroboros_workflows`: return the workflow manifest

## Core Runtime Concepts

```text
GoalContract -> Plan -> HITL Gate -> Execute -> EvidenceLedger
             -> Verifier Pool -> Arbiter Gate -> Replan / Stop / Complete
```

Main modules:

- `contracts.py`: risk classes, permissions, evidence requirements, verifier decisions
- `ledger.py`: append-only JSONL ledger with hash chaining and artifact hashing
- `gates.py`: plan/action/completion HITL gates
- `browser_adapter.py`: generic A11y snapshot/ref-action/diff adapter
- `agent_bootstrap.py`: Claude/Codex project automation installer
- `mcp_server.py`: minimal local stdio tool server

## Browser/A11y Setup

The browser adapter is driver-agnostic. Point it at any CLI that supports
snapshot/action-style commands:

```powershell
$env:OUROBOROS_BROWSER_BIN="C:\path\to\browser-driver.exe"
```

For Playwright accessibility checks in downstream projects:

```powershell
npm install -D @playwright/test @axe-core/playwright
npx playwright install
```

Optional CI tools:

```powershell
npm install -D pa11y lighthouse
```

## Development

Install this package locally:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

Run tests:

```powershell
$env:PYTHONPATH="src"
python -m unittest discover -s tests
```

Inspect current context:

```powershell
ouroboros-hitl context --pretty
```

## Repository Contents

```text
docs/
  agent_automation.md
  installation.md
  ouroboros_integration.md
  adr/
    0001-task-contract-as-binding-rules.md
    0002-three-layer-control-stack.md
examples/
  goal_contract.json
plugins/
  ouroboros-workflows/manifest.json
src/ouroboros_hitl/
  agent_bootstrap.py
  browser_adapter.py
  cli.py
  contracts.py
  gates.py
  ledger.py
  mcp_server.py
  orchestrator.py
  workflows.py
tests/
```
