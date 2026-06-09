from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import (
    EvidenceKind,
    EvidenceRequirement,
    GateDecision,
    GoalContract,
    VerifierDecision,
)
from causality.memory import TypedMemory
from causality.orchestrator import Causality
from causality.reflect import Reflection, reflect_on_contract


class ReflectTests(unittest.TestCase):
    def _setup(self, temp_dir: str):
        runtime = Causality(Path(temp_dir) / "ledger.jsonl")
        memory = TypedMemory(Path(temp_dir))
        contract = runtime.create_contract(
            GoalContract(
                "Ship feature",
                "implement and verify",
                evidence_required=[EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "tests")],
                stopping_policy={"max_iterations": 5},
            )
        )
        # Run: 1 evidence, 2 passing verifiers, 1 critical failing verifier.
        runtime.record_evidence(contract, EvidenceKind.TEST_OUTPUT, {"output": "ok"})
        runtime.record_verifier(contract, VerifierDecision("correctness", "pass", "looks right"))
        runtime.record_verifier(contract, VerifierDecision("evidence", "pass", "evidence present"))
        runtime.record_verifier(
            contract,
            VerifierDecision("safety", "fail", "unsafe path", severity="critical"),
        )
        # Run complete() once early -> a REPAIR gate decision lands in the ledger
        # (critical verifier failure remains unresolved).
        result = runtime.complete(contract)
        self.assertEqual(result.decision, GateDecision.REPAIR)
        return runtime, memory, contract

    def test_retrospective_written_once_with_provenance_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, memory, contract = self._setup(temp_dir)
            expected_hash = runtime.ledger.latest_hash()

            reflection = reflect_on_contract(runtime.ledger, memory, contract)

            self.assertIsInstance(reflection, Reflection)
            retros = memory.entries("retrospectives")
            self.assertEqual(len(retros), 1)
            entry = retros[0]
            self.assertEqual(entry, reflection.retrospective)

            # Provenance is the ledger's latest hash at reflect time (non-empty).
            self.assertTrue(entry.provenance)
            self.assertEqual(entry.provenance, expected_hash)

            # Summary mentions the distilled counts.
            summary = entry.summary
            self.assertIn("1 evidence", summary)
            self.assertIn("2 pass", summary)
            self.assertIn("1 fail", summary)
            self.assertIn("repair=1", summary)
            self.assertIn(contract.goal_id, summary)

    def test_failures_captured_for_failing_verifier_and_repair_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, memory, contract = self._setup(temp_dir)

            reflection = reflect_on_contract(runtime.ledger, memory, contract)

            failures = memory.entries("failures")
            # One for the failing verifier, one for the repair gate decision.
            self.assertEqual(len(failures), 2)
            self.assertEqual(len(reflection.failures), 2)

            scope = f"contract:{contract.goal_id}"
            for failure in failures:
                self.assertEqual(failure.metadata["scope"], scope)
                self.assertTrue(failure.provenance)

            summaries = [f.summary for f in failures]
            self.assertTrue(any("critical verifier failure" in s for s in summaries))
            self.assertTrue(any("repair gate decision" in s for s in summaries))

    def test_reflect_writes_no_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, memory, contract = self._setup(temp_dir)

            reflect_on_contract(runtime.ledger, memory, contract)

            # Governance (ADR 0005 §2.5): Reflect never launders a judgement
            # into a decision.
            self.assertEqual(memory.entries("decisions"), [])
            self.assertEqual(memory.entries("assumptions"), [])

    def test_retrospective_provenance_is_contract_scoped(self) -> None:
        # codex review r3382219479: provenance must point at THIS contract's last
        # event, not the global latest hash (which may belong to another contract).
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, memory, contract = self._setup(temp_dir)
            a_events = [e for e in runtime.ledger.events() if e.contract_id == contract.goal_id]
            a_last_hash = a_events[-1].entry_hash

            # A second contract records a LATER event -> global latest is now B's.
            other = runtime.create_contract(GoalContract("Other", "noise"))
            runtime.record_verifier(
                other, VerifierDecision("safety", "fail", "later", severity="critical")
            )
            self.assertNotEqual(runtime.ledger.latest_hash(), a_last_hash)

            reflection = reflect_on_contract(runtime.ledger, memory, contract)

            self.assertEqual(reflection.retrospective.provenance, a_last_hash)
            self.assertNotEqual(
                reflection.retrospective.provenance, runtime.ledger.latest_hash()
            )

    def test_reflect_ignores_other_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, memory, contract = self._setup(temp_dir)

            # A second contract's failure must not leak into the first's reflection.
            other = runtime.create_contract(GoalContract("Other", "noise"))
            runtime.record_verifier(
                other,
                VerifierDecision("safety", "fail", "other failure", severity="critical"),
            )

            reflection = reflect_on_contract(runtime.ledger, memory, contract)

            scope = f"contract:{contract.goal_id}"
            self.assertEqual(len(reflection.failures), 2)
            for failure in reflection.failures:
                self.assertEqual(failure.metadata["scope"], scope)


if __name__ == "__main__":
    unittest.main()
