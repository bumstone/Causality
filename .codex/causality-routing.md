# Causality Routing For Codex

Codex does not require project slash-command files for this integration. Use
`AGENTS.md` and `.causality/agent-rules.md` as the automatic router.

When the user asks for planning, implementation, debugging, browser/UI testing,
or completion, select the matching Causality workflow without waiting for the
user to name it:

- `causality-plan`
- `causality-verify`
- `causality-root-cause`
- `causality-a11y-observe`
- `causality-complete`

Use the MCP-style server when available:

```text
python -m causality.mcp_server --project .
```
