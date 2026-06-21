from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from causality.contracts import EvidenceKind, GoalContract, VerifierDecision
from causality.orchestrator import Causality
from causality.skills import SkillCandidate, SkillPromotionError, SkillStep, SkillStore


class SkillStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.causality = Causality(self.root / "ledger.jsonl")
        self.store = SkillStore(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_contract(self, title: str = "Ship the login fix") -> GoalContract:
        contract = GoalContract(title=title, summary="repair the broken flow")
        self.causality.create_contract(contract)
        self.causality.record_evidence(
            contract, EvidenceKind.TEST_OUTPUT, {"passed": True}
        )
        self.causality.record_verifier(
            contract,
            VerifierDecision(verifier="v1", status="pass", rationale="all green"),
        )
        return contract

    def test_distill_builds_ordered_steps_and_persists(self) -> None:
        contract = self._make_contract()
        candidate = self.store.distill(self.causality.ledger, contract)

        # steps are non-empty, ordered, structured procedures -- not "type:kind".
        self.assertTrue(candidate.steps)
        self.assertEqual(candidate.steps[0].action, "goal_contract")
        self.assertEqual(candidate.steps[1].action, "evidence")
        self.assertEqual(candidate.steps[1].outcome, "test_output")
        self.assertIn(("passed", "True"), candidate.steps[1].args)
        self.assertEqual(candidate.steps[2].action, "verifier_decision")
        self.assertEqual(candidate.steps[2].tool, "v1")  # binds the verifier
        self.assertEqual(candidate.steps[2].outcome, "pass")
        self.assertEqual(candidate.objective, "Ship the login fix")

        # provenance defaults to the last matching event's entry_hash.
        last_event = self.causality.ledger.events()[-1]
        self.assertEqual(candidate.provenance, last_event.entry_hash)

        self.assertEqual(candidate.attempts, 0)
        self.assertEqual(candidate.successes, 0)

        # persisted and visible via candidates().
        listed = self.store.candidates()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].skill_id, candidate.skill_id)

    def test_distill_explicit_provenance_overrides(self) -> None:
        contract = self._make_contract()
        candidate = self.store.distill(
            self.causality.ledger, contract, provenance="manual-ref"
        )
        self.assertEqual(candidate.provenance, "manual-ref")

    def test_distill_without_events_raises(self) -> None:
        contract = GoalContract(title="orphan", summary="no ledger events")
        with self.assertRaises(SkillPromotionError):
            self.store.distill(self.causality.ledger, contract)

    def test_record_outcome_increments(self) -> None:
        contract = self._make_contract()
        candidate = self.store.distill(self.causality.ledger, contract)

        after_success = self.store.record_outcome(candidate.skill_id, success=True)
        self.assertEqual(after_success.attempts, 1)
        self.assertEqual(after_success.successes, 1)

        after_failure = self.store.record_outcome(candidate.skill_id, success=False)
        self.assertEqual(after_failure.attempts, 2)
        self.assertEqual(after_failure.successes, 1)

        # candidates() returns the latest authoritative state, not duplicates.
        listed = self.store.candidates()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].attempts, 2)
        self.assertEqual(listed[0].successes, 1)

    def test_record_outcome_unknown_id_raises(self) -> None:
        with self.assertRaises(SkillPromotionError):
            self.store.record_outcome("does-not-exist", success=True)

    def _ready_candidate(self) -> SkillCandidate:
        contract = self._make_contract()
        candidate = self.store.distill(self.causality.ledger, contract)
        # 3 attempts, 2 successes -> meets default n-of-m.
        self.store.record_outcome(candidate.skill_id, success=True)
        self.store.record_outcome(candidate.skill_id, success=True)
        return self.store.record_outcome(candidate.skill_id, success=False)

    def test_promote_requires_approved_by(self) -> None:
        candidate = self._ready_candidate()
        with self.assertRaises(SkillPromotionError):
            self.store.promote(candidate.skill_id, approved_by="")
        with self.assertRaises(SkillPromotionError):
            self.store.promote(candidate.skill_id, approved_by="   ")

    def test_promote_requires_reproducibility(self) -> None:
        contract = self._make_contract()
        candidate = self.store.distill(self.causality.ledger, contract)
        # only one attempt/success -> below both thresholds.
        self.store.record_outcome(candidate.skill_id, success=True)

        with self.assertRaises(SkillPromotionError):
            self.store.promote(candidate.skill_id, approved_by="alice")

    def test_promote_requires_min_attempts(self) -> None:
        contract = self._make_contract()
        candidate = self.store.distill(self.causality.ledger, contract)
        # two successes but only two attempts -> attempts below default min (3).
        self.store.record_outcome(candidate.skill_id, success=True)
        self.store.record_outcome(candidate.skill_id, success=True)

        with self.assertRaises(SkillPromotionError):
            self.store.promote(candidate.skill_id, approved_by="alice")

    def test_promote_rejects_authored_duplicate(self) -> None:
        candidate = self._ready_candidate()
        with self.assertRaises(SkillPromotionError):
            self.store.promote(
                candidate.skill_id,
                approved_by="alice",
                authored_names=("ship the LOGIN fix",),  # case-insensitive match
            )

    def test_promote_succeeds_when_all_criteria_met(self) -> None:
        candidate = self._ready_candidate()
        promoted = self.store.promote(
            candidate.skill_id,
            approved_by="alice",
            authored_names=("test-driven-development",),
        )
        self.assertEqual(promoted.skill_id, candidate.skill_id)

        listed = self.store.promoted()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].skill_id, candidate.skill_id)
        self.assertEqual(listed[0].successes, 2)
        self.assertEqual(listed[0].attempts, 3)

    def test_promote_unknown_id_raises(self) -> None:
        with self.assertRaises(SkillPromotionError):
            self.store.promote("nope", approved_by="alice")

    def test_serialization_round_trip(self) -> None:
        candidate = SkillCandidate(
            skill_id="abc",
            objective="do the thing",
            steps=(
                SkillStep(action="evidence", outcome="test_output", args=(("passed", "True"),)),
                SkillStep(action="verifier_decision", tool="v1", outcome="pass"),
            ),
            provenance="hash",
            attempts=3,
            successes=2,
        )
        self.assertEqual(SkillCandidate.from_dict(candidate.to_dict()), candidate)

    def test_legacy_string_steps_load(self) -> None:
        # Skills distilled before structured steps stored "event_type:outcome"
        # strings; from_dict must still load them as SkillStep (back-compat).
        legacy = {
            "skill_id": "old",
            "objective": "legacy",
            "steps": ["evidence:test_output", "verifier_decision:"],
        }
        candidate = SkillCandidate.from_dict(legacy)
        self.assertEqual(candidate.steps[0], SkillStep(action="evidence", outcome="test_output"))
        self.assertEqual(candidate.steps[1], SkillStep(action="verifier_decision"))

    def test_distill_binds_artifacts(self) -> None:
        contract = GoalContract(title="ship", summary="with artifact")
        self.causality.create_contract(contract)
        report = self.root / "report.txt"
        report.write_text("ok", encoding="utf-8")
        self.causality.record_evidence(
            contract, EvidenceKind.ARTIFACT_HASH, {"note": "build"}, artifact_paths=[report]
        )
        candidate = self.store.distill(self.causality.ledger, contract)
        step = candidate.steps[-1]
        self.assertEqual(step.action, "evidence")
        self.assertEqual(len(step.artifacts), 1)
        self.assertTrue(step.artifacts[0].startswith(str(report)))
        self.assertIn("@", step.artifacts[0])  # path@sha256[:12]

    def test_distill_redacts_sensitive_payload(self) -> None:
        contract = GoalContract(title="ship", summary="with secret")
        self.causality.create_contract(contract)
        self.causality.record_evidence(
            contract, EvidenceKind.TOOL_OUTPUT, {"api_key": "sk-secret-123", "passed": True}
        )
        candidate = self.store.distill(self.causality.ledger, contract)
        step = candidate.steps[-1]
        self.assertIn(("api_key", "<redacted>"), step.args)
        # The secret value must not survive anywhere in the serialized skill.
        self.assertNotIn("sk-secret-123", json.dumps(candidate.to_dict()))

    def test_distill_redacts_secret_shaped_value_under_benign_key(self) -> None:
        # Security hardening: a secret in value position under a benign key must
        # still be masked before it enters the shared skill library.
        contract = GoalContract(title="ship", summary="benign key, secret value")
        self.causality.create_contract(contract)
        self.causality.record_evidence(
            contract, EvidenceKind.TOOL_OUTPUT, {"output": "auth=sk-ABCDEFGHIJ1234567890 ok"}
        )
        candidate = self.store.distill(self.causality.ledger, contract)
        self.assertIn(("output", "<redacted>"), candidate.steps[-1].args)
        self.assertNotIn("sk-ABCDEFGHIJ1234567890", json.dumps(candidate.to_dict()))

    def test_distill_redacts_hyphenated_sk_token_and_not_plain_words(self) -> None:
        # codex r3447999379: current sk- variants (sk-proj-/sk-svcacct-) carry
        # hyphens; they must redact, while an ordinary word containing "sk-"
        # ("task-management-system") must NOT be treated as a secret.
        contract = GoalContract(title="ship", summary="hyphenated token")
        self.causality.create_contract(contract)
        self.causality.record_evidence(
            contract,
            EvidenceKind.TOOL_OUTPUT,
            {"output": "key=sk-proj-AbCdEf0123456789ghIJ done", "module": "task-management-system"},
        )
        candidate = self.store.distill(self.causality.ledger, contract)
        args = dict(candidate.steps[-1].args)
        self.assertEqual(args["output"], "<redacted>")
        self.assertNotIn("sk-proj-AbCdEf0123456789ghIJ", json.dumps(candidate.to_dict()))
        self.assertEqual(args["module"], "task-management-system")  # not a false positive

    def test_distill_redacts_secret_in_tool_field(self) -> None:
        # codex r3448006271: a secret in a command/tool field is extracted into
        # SkillStep.tool and excluded from args, so it must be redacted there too.
        contract = GoalContract(title="ship", summary="secret in command")
        self.causality.create_contract(contract)
        self.causality.record_evidence(
            contract,
            EvidenceKind.TOOL_OUTPUT,
            {"command": "curl -H 'Authorization: Bearer sk-ABCDEFGHIJ1234567890'"},
        )
        candidate = self.store.distill(self.causality.ledger, contract)
        self.assertEqual(candidate.steps[-1].tool, "<redacted>")
        self.assertNotIn("sk-ABCDEFGHIJ1234567890", json.dumps(candidate.to_dict()))

    def test_distill_truncates_long_value(self) -> None:
        contract = GoalContract(title="ship", summary="with long value")
        self.causality.create_contract(contract)
        self.causality.record_evidence(
            contract, EvidenceKind.TOOL_OUTPUT, {"blob": "x" * 500}
        )
        candidate = self.store.distill(self.causality.ledger, contract)
        blob = dict(candidate.steps[-1].args)["blob"]
        self.assertLessEqual(len(blob), 80)
        self.assertTrue(blob.endswith("..."))

    def test_promote_rejects_near_duplicate_authored(self) -> None:
        candidate = self._ready_candidate()  # objective "Ship the login fix"
        with self.assertRaises(SkillPromotionError):
            self.store.promote(
                candidate.skill_id,
                approved_by="alice",
                authored_names=("ship login fix",),  # reworded near-duplicate
            )

    def test_promoted_empty_when_absent(self) -> None:
        self.assertEqual(self.store.promoted(), [])
        self.assertEqual(self.store.candidates(), [])


if __name__ == "__main__":
    unittest.main()
