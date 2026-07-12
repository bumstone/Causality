**English** | [Korean](README.ko.md)

# Causality

Causality is a local-first control harness for agent-driven work. It binds an
agent run to a goal contract, enforces gates before risky work, records evidence
in an append-only ledger, and feeds verified outcomes back into memory and skill
reuse.

The short version: Causality is not a chat prompt pack. It is a small Python
runtime for making agent work auditable.

## Current Status

The core control path is implemented and covered by the regression suite. See
[Project Summary](docs/project-summary.md) for the current verified baseline.

- Core control loop: implemented.
- Plan/action/tool/non-goal gates: enforced through `run_task`,
  `ExecutionAdapter`, and the file/subprocess `ToolAdapter`.
- HTTP and browser actions: explicit capability and scope policy routes both
  through the persistent task lifecycle and evidence ledger.
- Failure feedback: scoped failures can be approved into later `non_goals` and
  expire by TTL.
- Skill reuse: promoted earned skills are recalled by objective relevance and
  surfaced to execution.
- Verifier quality: hollow passes and blank evidence refs no longer satisfy the
  completion quorum.
- Secret safety: skill distillation redacts sensitive keys, nested structures,
  common token shapes, and bearer/basic authorization values.

Do not read this as “the product is finished.” Remaining work is executable
playbook phases, durable resume/skill operations, and external automation setup.

Canonical references:

- [Project Summary](docs/project-summary.md) — compact architecture and status.
- [Status Dashboard](docs/status/roadmap.html) — visual state board.
- [Delivery Plans](docs/plans/README.md) — numbered implementation order.
- [Implementation Specs](docs/specs/README.md) — feature contracts and acceptance tests.
- [ADR Index](docs/adr/README.md) — design decision history.

## Architecture

Causality separates the run into five layers:

1. **L0 Identity and Memory** — agenda, scoped memory, authored and earned
   skills.
2. **L1 Dispatch** — task-type routing with fail-safe handling for sensitive
   unmatched requests.
3. **L2 Contract** — frozen task contract: objective, non-goals, tools,
   verification, stop condition, escalation.
4. **L3 Execution Control** — plan/action/completion gates, bounded loop,
   review and verifier decisions.
5. **L4 Evidence Ledger** — hash-chained JSONL evidence with durable writes and
   size-guarded read caches.

Control flows L0 → L4. Reflection flows back from ledger evidence into memory
and skills.

## What It Installs

`causality install-agent` installs thin project-level agent files:

- `AGENTS.md` for Codex routing.
- `CLAUDE.md` and `.claude/commands/*` for Claude command ergonomics.
- `.causality/agent-rules.md`, workflow manifest, MCP config, and a local
  ledger.
- `workflow/`, `checklists/`, `skills/`, and `memory/` on-demand context,
  including `/onboard` via `skills/onboard-project.md`.

Generated workflow files are views of `src/causality/workflows.py`. Avoid
hand-editing generated views unless you are intentionally customizing a local
project install.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
python -m unittest discover -s tests
```

Windows one-command local checkout:

```powershell
git clone https://github.com/bumstone/Causality.git D:\dev\Causality
cd D:\dev\Causality
.\scripts\install.ps1
```

Linux/WSL:

```bash
git clone https://github.com/bumstone/Causality.git ~/dev/Causality
cd ~/dev/Causality
bash scripts/install.sh
```

## CLI

```powershell
causality init
causality context --pretty
causality manifest --pretty
causality install-agent --client codex --adopt --verify
causality review-plan
causality doc-budget --enforce docs/project-summary.md
```

For MCP-style clients:

```powershell
python -I -m causality.mcp_server --project .
```

## Repository Map

```text
src/causality/        runtime package
tests/                regression suite
docs/project-summary.md
docs/status/          current status board
docs/plans/           numbered delivery order
docs/specs/           implementation contracts
docs/adr/             decision history
workflow/             generated workflow views
scripts/              install/update/doctor helpers
```

## Development Rules

- Keep runtime behavior covered by tests. Passing tests are regression assets,
  not disposable scaffolding.
- Keep current status in one place: `docs/project-summary.md` plus
  `docs/status/roadmap.html`.
- Keep ADRs as decision history; do not use ADR index pages as live status
  dashboards.
- Generated install artifacts should point to canonical rules instead of
  duplicating long explanations.

## License and Attribution

This repository is an original implementation under the [MIT LICENSE](LICENSE).
It does not vendor upstream source. Referenced upstream projects are credited in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
