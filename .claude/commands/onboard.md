---
description: Gather project context, current work, priorities, and next actions with managed subagents.
---

Use `.causality/agent-rules.md` and `skills/onboard-project.md`.

Focus: $ARGUMENTS

Run the session-bootstrap flow, spawn bounded read-only subagents for repo map,
current work, plan priorities, and verification/risk when available, synthesize
the reports in the main agent, and close every subagent before responding. Do
not edit code during onboarding unless the user separately asks for
implementation.
