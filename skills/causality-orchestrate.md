# Skill: causality-orchestrate

Use for end-to-end execution or requests to continue until a task is terminal.

## Bootstrap contract

The installed Python package and `causality install-agent --client <name>
--verify` are the official pre-session bootstrap. Once MCP is loaded, call
`causality_init(client=<name>, verify=true)`. Continue only on `active`; return
its exact remediation and stop on `pending|broken`. Then read `tools/list` and
never guess an unadvertised capability.

## Restart-safe loop

1. Begin or resume one task; acquire its controller lease.
2. Refresh resume state before every mutation.
3. Execute only `task.recommended_next`; persist a secret-free checkpoint.
4. Host supplies edits/actions. Humans alone supply approval or recovery proof.
   Independent verifiers supply their own cited decisions.
5. Never replay an uncertain effect. Stop on pending intent for HITL resolution.
6. Complete through the server gate, reflect once, release the lease, and return
   task ID plus final event hash.

Never store proof, credentials, browser text, raw HTTP bodies, or ledger payloads.
The controller lease coordinates writers; it is not authentication.
