# Workflow: session-bootstrap

> Generated view of `workflows.py` (single source). Do not edit by hand.

Load only active seed, ledger tail, relevant memory, and current permissions.

- Layer: stage_designer
- Required inputs: active_seed, ledger_tail, memory_facts, permissions
- Outputs: context_packet, open_questions, allowed_next_actions
- Gate: context_sufficiency_check

## Notes

- Do not inject entire skill libraries every turn
- Only verified facts enter memory
