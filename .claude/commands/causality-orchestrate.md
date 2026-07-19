---
description: Drive the durable Causality lifecycle until terminal or an explicit handoff.
---

Use `skills/causality-orchestrate.md` and the advertised MCP tools only.

Objective: $ARGUMENTS

Run the restart-safe causality-orchestrate loop. Stop for bootstrap remediation,
missing capability, uncertain effects, HITL proof, host work, or independent
verifier input. Never invent a decision or persist a secret.
