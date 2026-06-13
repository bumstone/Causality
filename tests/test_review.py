from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import AuditEventType, GoalContract, VerifierDecision
from causality.orchestrator import Causality
from causality.review import ReviewResult, Verifier, run_review


def _pass(name: str) -> Verifier:
    return lambda contract: VerifierDecision(name, "pass", "ok")


def _fail(name: str, severity: str = "normal") -> Verifier:
    return lambda contract: VerifierDecision(name, "fail", "nope", severity=severity)


class ReviewTests(unittest.TestCase):
    def _runtime(self, temp_dir: str) -> Causality:
        return Causality(Path(temp_dir) / "ledger.jsonl")

    def _contract(self, runtime: Causality) -> GoalContract:
        return runtime.create_contract(GoalContract("Review", "review pass"))

    def test_two_passes_are_approved_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(temp_dir)
            contract = self._contract(runtime)

            result = run_review(
                runtime,
                contract,
                [_pass("correctness"), _pass("evidence")],
            )

            self.assertTrue(result.approved)
            self.assertEqual(result.passes, 2)
            self.assertFalse(result.has_critical_failure)
            self.assertEqual(len(result.decisions), 2)
            self.assertEqual(
                [d.verifier for d in result.decisions],
                ["correctness", "evidence"],
            )

            recorded = runtime.ledger.find(AuditEventType.VERIFIER_DECISION)
            self.assertEqual(len(recorded), 2)

    def test_critical_failure_blocks_even_with_enough_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(temp_dir)
            contract = self._contract(runtime)

            result = run_review(
                runtime,
                contract,
                [_pass("a"), _pass("b"), _fail("security", severity="critical")],
            )

            self.assertEqual(result.passes, 2)
            self.assertTrue(result.has_critical_failure)
            self.assertFalse(result.approved)

            recorded = runtime.ledger.find(AuditEventType.VERIFIER_DECISION)
            self.assertEqual(len(recorded), 3)

    def test_fewer_than_min_passes_is_not_approved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(temp_dir)
            contract = self._contract(runtime)

            result = run_review(
                runtime,
                contract,
                [_pass("only"), _fail("other")],
            )

            self.assertEqual(result.passes, 1)
            self.assertFalse(result.has_critical_failure)
            self.assertFalse(result.approved)

    def test_custom_min_passes_is_respected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(temp_dir)
            contract = self._contract(runtime)

            # One pass clears a min_passes of 1.
            approved = run_review(runtime, contract, [_pass("solo")], min_passes=1)
            self.assertEqual(approved.passes, 1)
            self.assertTrue(approved.approved)

            # The default of two passes falls short of a min_passes of 3.
            blocked = run_review(
                runtime,
                contract,
                [_pass("a"), _pass("b")],
                min_passes=3,
            )
            self.assertEqual(blocked.passes, 2)
            self.assertFalse(blocked.approved)

    def test_to_dict_round_trips_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(temp_dir)
            contract = self._contract(runtime)

            result = run_review(runtime, contract, [_pass("a"), _pass("b")])
            payload = result.to_dict()

            self.assertEqual(payload["passes"], 2)
            self.assertEqual(payload["approved"], True)
            self.assertEqual(payload["has_critical_failure"], False)
            self.assertEqual(len(payload["decisions"]), 2)
            self.assertIsInstance(result, ReviewResult)

    def test_no_verifiers_is_not_approved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(temp_dir)
            contract = self._contract(runtime)

            result = run_review(runtime, contract, [])

            self.assertEqual(result.passes, 0)
            self.assertFalse(result.approved)
            self.assertEqual(result.decisions, ())
            self.assertEqual(runtime.ledger.find(AuditEventType.VERIFIER_DECISION), [])

    def test_duplicate_verifier_names_count_once(self) -> None:
        # codex review r3407165600: two callbacks sharing a verifier name must
        # not fake two independent passes (which would keep approved/progress
        # true forever and hang a no-progress-bounded loop).
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(temp_dir)
            contract = self._contract(runtime)

            result = run_review(runtime, contract, [_pass("correctness"), _pass("correctness")], min_passes=2)

            self.assertEqual(result.passes, 1)  # one distinct verifier
            self.assertFalse(result.approved)
            # Both raw decisions are still recorded in the ledger.
            self.assertEqual(len(runtime.ledger.find(AuditEventType.VERIFIER_DECISION)), 2)


if __name__ == "__main__":
    unittest.main()
