from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ouroboros_hitl.contracts import EvidenceKind, EvidenceRequirement, GoalContract, Risk


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


if __name__ == "__main__":
    unittest.main()
