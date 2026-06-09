from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ouroboros_hitl import ContractHarness, ContractHarnessError, OuroborosHITL
from ouroboros_hitl.contracts import AuditEventType


class ContractHarnessTests(unittest.TestCase):
    def _harness(self, temp_dir: str) -> tuple[OuroborosHITL, ContractHarness]:
        runtime = OuroborosHITL(Path(temp_dir) / "ledger.jsonl")
        return runtime, ContractHarness(runtime)

    def test_bind_produces_task_contract_and_records_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, harness = self._harness(temp_dir)

            tc = harness.bind(
                objective="Add non_goals enforcement",
                summary="thin slice",
                verification=["python -m unittest discover -s tests"],
                stop_condition={"max_iterations": 3},
                non_goals=["refactor browser_adapter", "  "],
                allowed_tools=["Edit", "Bash"],
            )

            self.assertEqual(tc.non_goals, ("refactor browser_adapter",))
            self.assertEqual(tc.allowed_tools, ("Edit", "Bash"))
            self.assertEqual(tc.verification, ("python -m unittest discover -s tests",))

            events = runtime.ledger.find(AuditEventType.GOAL_CONTRACT)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].payload["non_goals"], ["refactor browser_adapter"])
            self.assertTrue(runtime.ledger.verify_chain())

    def test_bind_requires_objective(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _, harness = self._harness(temp_dir)
            with self.assertRaises(ContractHarnessError):
                harness.bind(objective="   ", verification=["x"], stop_condition={"max_iterations": 1})

    def test_bind_requires_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _, harness = self._harness(temp_dir)
            with self.assertRaises(ContractHarnessError):
                harness.bind(objective="o", verification=[], stop_condition={"max_iterations": 1})

    def test_bind_requires_stop_condition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _, harness = self._harness(temp_dir)
            with self.assertRaises(ContractHarnessError):
                harness.bind(objective="o", verification=["x"], stop_condition={})


if __name__ == "__main__":
    unittest.main()
