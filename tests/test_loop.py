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
from causality.memory import TypedMemory
from causality.reflect import reflect_on_contract


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
                evidence = runtime.record_evidence(
                    c, EvidenceKind.TEST_OUTPUT, {"output": "ok"}
                )
                refs = (evidence.entry_hash,)
                runtime.record_verifier(
                    c, VerifierDecision("correctness", "pass", "ok", evidence_refs=refs)
                )
                runtime.record_verifier(
                    c, VerifierDecision("evidence", "pass", "ok", evidence_refs=refs)
                )
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

    def test_should_stop_polls_do_not_pollute_reflect_gate_counts(self) -> None:
        # Observer effect: a multi-iteration loop polls should_stop before
        # every iteration. Those "keep going" polls must not be distilled by
        # Reflect as gate passes -- only the terminal completion PASS is a real
        # pass. Before the fix, two should_stop polls would push pass=1 to pass=3.
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = self._runtime(temp_dir)
            memory = TypedMemory(Path(temp_dir))
            contract = runtime.create_contract(
                GoalContract(
                    "Loop", "repairs once then completes",
                    evidence_required=[EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "tests")],
                    stopping_policy={"max_iterations": 5},
                )
            )

            def step(c: GoalContract, i: int) -> StepOutcome:
                # Satisfy completion only on the second iteration: iter 0 -> a
                # REPAIR gate decision, iter 1 -> the terminal completion PASS.
                if i >= 1:
                    evidence = runtime.record_evidence(
                        c, EvidenceKind.TEST_OUTPUT, {"output": "ok"}
                    )
                    refs = (evidence.entry_hash,)
                    runtime.record_verifier(
                        c,
                        VerifierDecision("correctness", "pass", "ok", evidence_refs=refs),
                    )
                    runtime.record_verifier(
                        c,
                        VerifierDecision("evidence", "pass", "ok", evidence_refs=refs),
                    )
                return StepOutcome(progress=True)

            result = run_bounded_loop(runtime, contract, step)
            self.assertEqual(result.decision, GateDecision.PASS)
            self.assertEqual(result.iterations, 2)

            reflection = reflect_on_contract(runtime.ledger, memory, contract)
            summary = reflection.retrospective.summary
            # Exactly one real gate pass (the terminal complete) and one repair.
            self.assertIn("pass=1", summary)
            self.assertIn("repair=1", summary)


if __name__ == "__main__":
    unittest.main()
