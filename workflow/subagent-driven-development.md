# Workflow: subagent-driven-development

> Generated view of `workflows.py` (single source). Do not edit by hand.

Assign fresh bounded task packets to subagents while controller retains orchestration.

- Layer: stage_designer
- Required inputs: seed_id, task_id, allowed_tools, context_packet
- Outputs: subagent_report, evidence_refs, uncertainties
- Gate: subagent_output_verifier_review

## Notes

- Do not share full session context
- Use disjoint write scopes for parallel workers
