# Workflow: root-cause-protocol

> Generated view of `workflows.py` (single source). Do not edit by hand.

Investigate and prove root cause before applying a fix.

- Layer: planner
- Required inputs: symptom, reproduction_steps, affected_scope
- Outputs: root_cause_hypothesis, confirming_evidence, fix_plan
- Gate: three_failed_hypotheses_escalation

## Notes

- After three failed hypotheses, escalate to HITL
- Avoid symptom-only fixes
