from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataclasses import FrozenInstanceError

from causality.contracts import (
    EvidenceKind,
    EvidenceRequirement,
    GoalContract,
    PermissionContract,
    Risk,
    TaskContract,
)


class ContractTests(unittest.TestCase):
    def test_high_risk_contract_requires_approval(self) -> None:
        contract = GoalContract(
            title="Deploy",
            summary="Deploy a high-risk change",
            risk=Risk.HIGH,
            evidence_required=[
                EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "test output"),
            ],
        )

        self.assertTrue(contract.approval_required)
        self.assertEqual(contract.required_evidence_kinds(), {"test_output"})
        self.assertEqual(contract.to_dict()["risk"], "high")

    def test_low_risk_contract_does_not_require_approval(self) -> None:
        contract = GoalContract(title="Docs", summary="Update docs", risk=Risk.LOW)

        self.assertFalse(contract.approval_required)

    def test_non_goals_roundtrip(self) -> None:
        contract = GoalContract(
            title="Slice",
            summary="thin",
            non_goals=("refactor browser_adapter", "touch CI config"),
        )

        data = contract.to_dict()
        self.assertEqual(data["non_goals"], ["refactor browser_adapter", "touch CI config"])

        restored = GoalContract.from_mapping(data)
        self.assertEqual(restored.non_goals, ("refactor browser_adapter", "touch CI config"))

    def test_task_contract_derives_clauses(self) -> None:
        contract = GoalContract(
            title="Add gates",
            summary="enforce clauses",
            risk=Risk.HIGH,
            permissions=PermissionContract(allowed_tools=("Edit", "Bash")),
            evidence_required=[EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "python -m unittest")],
            non_goals=("skip tests",),
            stopping_policy={"max_iterations": 3},
        )

        tc = TaskContract.of(contract)

        self.assertEqual(tc.objective, "Add gates: enforce clauses")
        self.assertEqual(tc.non_goals, ("skip tests",))
        self.assertEqual(tc.allowed_tools, ("Edit", "Bash"))
        self.assertEqual(tc.verification, ("python -m unittest",))
        self.assertEqual(tc.stop_condition["max_iterations"], 3)
        self.assertIn("final_approval", tc.escalation)
        self.assertEqual(tc.goal_id, contract.goal_id)

    def test_task_contract_is_immutable(self) -> None:
        tc = TaskContract.of(GoalContract(title="x", summary="y"))

        with self.assertRaises(FrozenInstanceError):
            tc.objective = "mutated"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            tc.stop_condition["max_iterations"] = 99  # read-only mapping


if __name__ == "__main__":
    unittest.main()
