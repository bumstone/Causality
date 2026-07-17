from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import EvidenceKind, GoalContract, VerifierDecision
from causality.orchestrator import Causality
from causality.skills import SkillPromotionError, SkillStore


class SkillOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = Causality(self.root / "ledger.jsonl", project_root=self.root)
        self.store = SkillStore(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _candidate(self, title: str = "Ship the durable login fix"):
        contract = GoalContract(title=title, summary="verified procedure")
        self.runtime.create_contract(contract)
        evidence = self.runtime.record_evidence(
            contract, EvidenceKind.TEST_OUTPUT, {"passed": True}
        )
        self.runtime.record_verifier(
            contract,
            VerifierDecision(
                "correctness", "pass", "independent review", evidence_refs=(evidence.entry_hash,)
            ),
        )
        return contract, self.store.distill_once(
            self.runtime.ledger,
            contract,
            skill_id="skill-deterministic-1",
            source_task_id=contract.goal_id,
        )

    def test_distill_once_is_deterministic_and_conflicts_fail_closed(self) -> None:
        contract, first = self._candidate()
        second = self.store.distill_once(
            self.runtime.ledger,
            contract,
            skill_id=first.skill_id,
            source_task_id=contract.goal_id,
        )
        self.assertEqual(second, first)
        with self.assertRaises(SkillPromotionError):
            self.store.distill_once(
                self.runtime.ledger,
                contract,
                skill_id=first.skill_id,
                provenance="different-provenance",
                source_task_id=contract.goal_id,
            )

    def test_outcome_retry_is_idempotent_and_conflicts(self) -> None:
        _, candidate = self._candidate()
        first = self.store.record_outcome(
            candidate.skill_id,
            success=True,
            attempt_id="task-attempt-1",
            evidence_refs=("a" * 64,),
        )
        replay = self.store.record_outcome(
            candidate.skill_id,
            success=True,
            attempt_id="task-attempt-1",
            evidence_refs=("a" * 64,),
        )
        self.assertEqual(replay, first)
        self.assertEqual(replay.attempts, 1)
        with self.assertRaises(SkillPromotionError):
            self.store.record_outcome(
                candidate.skill_id,
                success=False,
                attempt_id="task-attempt-1",
                evidence_refs=("a" * 64,),
            )

    def test_distill_retry_preserves_recorded_outcomes(self) -> None:
        contract, candidate = self._candidate()
        recorded = self.store.record_outcome(
            candidate.skill_id,
            success=True,
            attempt_id="task-attempt-1",
            evidence_refs=("a" * 64,),
        )

        replay = self.store.distill_once(
            self.runtime.ledger,
            contract,
            skill_id=candidate.skill_id,
            source_task_id=contract.goal_id,
        )

        self.assertEqual(replay, recorded)
        self.assertEqual(replay.attempts, 1)
        self.assertEqual(len(replay.outcomes), 1)

    def test_concurrent_same_attempt_counts_once(self) -> None:
        _, candidate = self._candidate()

        def record() -> int:
            return self.store.record_outcome(
                candidate.skill_id,
                success=True,
                attempt_id="task-concurrent",
                evidence_refs=("b" * 64,),
            ).attempts

        with ThreadPoolExecutor(max_workers=8) as pool:
            attempts = list(pool.map(lambda _: record(), range(8)))
        self.assertEqual(attempts, [1] * 8)
        self.assertEqual(self.store.candidates()[0].successes, 1)

    def test_promotion_retry_is_deduped_and_keeps_evidence(self) -> None:
        _, candidate = self._candidate()
        for index, success in enumerate((True, True, False), start=1):
            self.store.record_outcome(
                candidate.skill_id,
                success=success,
                attempt_id=f"task-{index}",
                evidence_refs=(str(index) * 64,),
            )
        promoted = self.store.promote(
            candidate.skill_id,
            approved_by="operator",
            authored_names=("unrelated-authored-skill",),
            evidence_refs=("c" * 64,),
        )
        replay = self.store.promote(
            candidate.skill_id,
            approved_by="operator",
            authored_names=("unrelated-authored-skill",),
            evidence_refs=("c" * 64,),
        )
        self.assertEqual(replay, promoted)
        self.assertEqual(len(self.store.promoted()), 1)
        self.assertEqual(promoted.promotion_evidence_refs, ("c" * 64,))


if __name__ == "__main__":
    unittest.main()
