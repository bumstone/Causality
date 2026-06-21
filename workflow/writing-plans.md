# Workflow: writing-plans

> Generated view of `workflows.py` (single source). Do not edit by hand.

Create path-specific plans with acceptance criteria and verification commands.

- Layer: stage_designer
- Required inputs: goal_contract, repo_context, constraints
- Outputs: immutable_plan_snapshot, acceptance_criteria, verification_commands
- Gate: goal_scope_or_high_risk_plan_approval

## Notes

- No placeholders
- Attach plan snapshot to the ledger before execution
