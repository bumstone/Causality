from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from causality.contracts import EvidenceKind, GoalContract, VerifierDecision
from causality.orchestrator import Causality
from causality.skills import SkillCandidate, SkillPromotionError, SkillStore


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

        # steps are non-empty, ordered, and reflect the ledger trajectory.
        self.assertTrue(candidate.steps)
        self.assertEqual(candidate.steps[0], "goal_contract:")
        self.assertEqual(candidate.steps[1], "evidence:test_output")
        self.assertEqual(candidate.steps[2], "verifier_decision:")
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
            steps=("a:1", "b:2"),
            provenance="hash",
            attempts=3,
            successes=2,
        )
        self.assertEqual(SkillCandidate.from_dict(candidate.to_dict()), candidate)

    def test_promoted_empty_when_absent(self) -> None:
        self.assertEqual(self.store.promoted(), [])
        self.assertEqual(self.store.candidates(), [])


if __name__ == "__main__":
    unittest.main()
