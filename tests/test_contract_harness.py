from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality import ContractHarness, ContractHarnessError, Causality
from causality.contracts import AuditEventType, GateDecision


class ContractHarnessTests(unittest.TestCase):
    def _harness(self, temp_dir: str) -> tuple[Causality, ContractHarness]:
        runtime = Causality(Path(temp_dir) / "ledger.jsonl")
        return runtime, ContractHarness(runtime)

    def test_bind_produces_task_contract_and_records_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, harness = self._harness(temp_dir)

            bound = harness.bind(
                objective="Add non_goals enforcement",
                summary="thin slice",
                verification=["python -m unittest discover -s tests"],
                stop_condition={"max_iterations": 3},
                non_goals=["refactor browser_adapter", "  "],
                allowed_tools=["Edit", "Bash"],
            )

            self.assertEqual(bound.task.non_goals, ("refactor browser_adapter",))
            self.assertEqual(bound.task.allowed_tools, ("Edit", "Bash"))
            self.assertEqual(bound.task.verification, ("python -m unittest discover -s tests",))

            events = runtime.ledger.find(AuditEventType.GOAL_CONTRACT)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].payload["non_goals"], ["refactor browser_adapter"])
            self.assertTrue(runtime.ledger.verify_chain())

    def test_bound_contract_feeds_the_enforcement_path(self) -> None:
        # The gateable GoalContract returned by the harness must flow directly
        # into the runtime gates (codex review r3381964877).
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, harness = self._harness(temp_dir)

            bound = harness.bind(
                objective="Scoped task",
                verification=["pytest"],
                stop_condition={"max_iterations": 2},
                allowed_tools=["Edit"],
                non_goals=["delete data"],
            )

            self.assertEqual(runtime.evaluate_plan(bound.contract).decision, GateDecision.PASS)
            self.assertEqual(runtime.check_tool_allowed(bound.contract, "Edit").decision, GateDecision.PASS)
            self.assertEqual(
                runtime.check_tool_allowed(bound.contract, "Curl").decision, GateDecision.ESCALATE
            )
            self.assertEqual(
                runtime.check_non_goal(bound.contract, "now delete data").decision, GateDecision.STOP
            )

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

    def test_bind_rejects_stop_condition_without_a_real_ceiling(self) -> None:
        # Regression F4 + codex r3407165600: stop_condition must guarantee
        # termination via a positive `max_iterations`. Irrelevant keys, zero, or
        # only a progress-dependent ceiling (no_progress_iterations) are rejected
        # because they cannot bound a loop whose progress signal may be wrong.
        with tempfile.TemporaryDirectory() as temp_dir:
            _, harness = self._harness(temp_dir)
            for bad in ({"foo": 1}, {"max_iterations": 0}, {"no_progress_iterations": 5}):
                with self.assertRaises(ContractHarnessError):
                    harness.bind(objective="o", verification=["x"], stop_condition=bad)
            # max_iterations present -> accepted; extra ceilings are allowed.
            bound = harness.bind(
                objective="o",
                verification=["x"],
                stop_condition={"max_iterations": 3, "no_progress_iterations": 2},
            )
            self.assertEqual(bound.task.stop_condition["max_iterations"], 3)


if __name__ == "__main__":
    unittest.main()
