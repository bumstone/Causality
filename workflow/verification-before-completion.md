# Workflow: verification-before-completion

> Generated view of `workflows.py` (single source). Do not edit by hand.

Block completion until fresh evidence proves the acceptance criteria.

- Layer: executor
- Required inputs: acceptance_criteria, evidence_requirements, ledger_tail
- Outputs: verification_report, missing_evidence, verifier_decisions
- Gate: completion_gate

## Notes

- Agent prose is a claim, not evidence
- Use raw tool output or artifact hashes
