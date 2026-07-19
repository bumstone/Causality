# Causality Routing For Codex

Codex does not require project slash-command files for this integration. Use
`AGENTS.md` and `.causality/agent-rules.md` as the automatic router.

When the user asks for planning, implementation, debugging, browser/UI testing,
completion, or explicit onboarding, select the matching Causality workflow
without waiting for the user to name it:

- `onboard` uses `skills/onboard-project.md`, `session-bootstrap`, and bounded
  subagent inspection when available
- `causality-plan`
- `causality-verify`
- `causality-root-cause`
- `causality-a11y-observe`
- `causality-orchestrate` uses `skills/causality-orchestrate.md` for end-to-end
  execution and resume-until-terminal requests
- `causality-complete`

Use the MCP-style server when available:

```text
python -m causality.mcp_server --project .
```
