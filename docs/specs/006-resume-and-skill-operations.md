# Spec 006 — Resume and Skill Operations

Status: implemented.

## Contract

Interrupted tasks resume from durable state without repeating effects. Earned
skills stay local, task-evidence-bound, and human-promoted.

## 006A — Resume and context

- `causality_task_resume(task_id)` is closed and read-only. It rebuilds the
  frozen contract, phase, fresh unmet checks, safe pending intent, and next
  actions from a chain-verified ledger.
- Terminal/reflected tasks return recorded results. Uncertain effects expose
  only human resolution; resume never guesses or replays them.
- Context returns metadata-only ledger tail, TTL-active failures, curated
  Markdown paths, and recommended runtime JSONL ignores.

## 006B — Skill evolution

- Verified reflection creates one server-derived candidate bounded by the
  reflection hash. Retry returns the same candidate after later outcomes.
- `causality_skill_outcome` accepts one terminal task once. Success must match
  verified/rejected state and cite the exact current verification evidence.
- `causality_skill_promote` uses fixed `2/3` successes/attempts, `0.6` authored
  dedup, exact successful-outcome evidence, and proof-backed named approval.
- `causality_skill_recall` returns authored skills first and only audited
  promoted skills. It is read-only and chain-verifies first.
- Outcome/promotion audit events omit proof and raw requests. Candidate and
  operation retries survive process restart; conflicting retries fail closed.

## Acceptance

Unit tests cover deterministic boundaries, concurrency, conflicts, thresholds,
dedup, and redaction. MCP tests cover exact evidence, restart replay, approval,
recall, and audit cardinality. A fresh-venv external-project stdio test runs the
three-attempt outcome → promotion → recall loop across two server processes.
