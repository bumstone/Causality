from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality import (
    EvidenceKind,
    EvidenceRequirement,
    GateDecision,
    GoalContract,
    Causality,
    Risk,
    StepOutcome,
    VerifierDecision,
    run_bounded_loop,
)


class LoopTests(unittest.TestCase):
    def _runtime(self, temp_dir: str) -> Causality:
        return Causality(Path(temp_dir) / "ledger.jsonl")

    def test_loop_passes_when_completion_criteria_met(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(temp_dir)
            contract = runtime.create_contract(
                GoalContract(
                    "Loop", "completes",
                    evidence_required=[EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "tests")],
                    stopping_policy={"max_iterations": 5},
                )
            )

            def step(c: GoalContract, i: int) -> StepOutcome:
                runtime.record_evidence(c, EvidenceKind.TEST_OUTPUT, {"output": "ok"})
                runtime.record_verifier(c, VerifierDecision("correctness", "pass", "ok"))
                runtime.record_verifier(c, VerifierDecision("evidence", "pass", "ok"))
                return StepOutcome(progress=True)

            result = run_bounded_loop(runtime, contract, step)

            self.assertEqual(result.decision, GateDecision.PASS)
            self.assertEqual(result.iterations, 1)

    def test_loop_stops_at_max_iterations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(temp_dir)
            contract = runtime.create_contract(
                GoalContract(
                    "Loop", "never completes",
                    evidence_required=[EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "tests")],
                    stopping_policy={"max_iterations": 2, "no_progress_iterations": 99},
                )
            )

            calls = {"n": 0}

            def step(c: GoalContract, i: int) -> StepOutcome:
                calls["n"] += 1
                return StepOutcome(progress=True)  # never records the required evidence

            result = run_bounded_loop(runtime, contract, step)

            self.assertEqual(result.decision, GateDecision.STOP)
            self.assertEqual(result.iterations, 2)
            self.assertEqual(calls["n"], 2)

    def test_loop_escalates_when_failed_hypotheses_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(temp_dir)
            contract = runtime.create_contract(
                GoalContract(
                    "Loop", "fails",
                    evidence_required=[EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "tests")],
                    stopping_policy={"max_iterations": 99, "no_progress_iterations": 99, "max_failed_hypotheses": 2},
                )
            )

            def step(c: GoalContract, i: int) -> StepOutcome:
                return StepOutcome(progress=True, failed_hypothesis=True)

            result = run_bounded_loop(runtime, contract, step)

            self.assertEqual(result.decision, GateDecision.ESCALATE)
            self.assertEqual(result.iterations, 2)

    def test_falsy_nonbool_step_counts_as_no_progress(self) -> None:
        # Regression H6: a step returning 0 (falsy non-bool) must register as
        # no-progress so the no-progress ceiling can fire, not be treated as
        # progress by an `isinstance(value, bool)`-only path.
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(temp_dir)
            contract = runtime.create_contract(
                GoalContract(
                    "Loop", "no progress",
                    evidence_required=[EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "tests")],
                    stopping_policy={"max_iterations": 99, "no_progress_iterations": 2},
                )
            )

            def step(c: GoalContract, i: int):
                return 0  # falsy non-bool: zero units changed -> no progress

            result = run_bounded_loop(runtime, contract, step)
            self.assertEqual(result.decision, GateDecision.STOP)
            self.assertEqual(result.iterations, 2)


if __name__ == "__main__":
    unittest.main()
