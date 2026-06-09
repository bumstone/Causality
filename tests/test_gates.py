from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ouroboros_hitl.contracts import (
    EvidenceKind,
    EvidenceRequirement,
    GateDecision,
    GoalContract,
    PermissionContract,
    Risk,
    VerifierDecision,
)
from ouroboros_hitl.orchestrator import OuroborosHITL


class GateTests(unittest.TestCase):
    def test_low_risk_completion_requires_evidence_and_two_verifiers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = OuroborosHITL(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(
                GoalContract(
                    title="Smoke test",
                    summary="Low-risk task",
                    risk=Risk.LOW,
                    evidence_required=[
                        EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "test output"),
                    ],
                )
            )

            self.assertEqual(runtime.evaluate_plan(contract).decision, GateDecision.PASS)
            self.assertEqual(runtime.complete(contract).decision, GateDecision.REPAIR)

            runtime.record_evidence(contract, EvidenceKind.TEST_OUTPUT, {"output": "passed"})
            runtime.record_verifier(contract, VerifierDecision("correctness", "pass", "tests passed"))
            runtime.record_verifier(contract, VerifierDecision("evidence", "pass", "evidence present"))

            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_high_risk_plan_and_final_require_human_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = OuroborosHITL(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(
                GoalContract(
                    title="Deploy",
                    summary="High-risk deploy",
                    risk=Risk.HIGH,
                    evidence_required=[
                        EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "test output"),
                    ],
                )
            )

            self.assertEqual(runtime.evaluate_plan(contract).decision, GateDecision.ESCALATE)
            runtime.approve(contract, "plan", "kbssk", "Plan reviewed")
            self.assertEqual(runtime.evaluate_plan(contract).decision, GateDecision.PASS)

            runtime.record_evidence(contract, EvidenceKind.TEST_OUTPUT, {"output": "passed"})
            runtime.record_verifier(contract, VerifierDecision("correctness", "pass", "tests passed"))
            runtime.record_verifier(contract, VerifierDecision("evidence", "pass", "evidence present"))
            self.assertEqual(runtime.complete(contract).decision, GateDecision.ESCALATE)

            runtime.approve(contract, "final", "kbssk", "Raw evidence reviewed")
            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_irreversible_action_requires_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = OuroborosHITL(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(GoalContract("Delete data", "Dangerous", Risk.LOW))

            self.assertEqual(runtime.can_execute_action(contract, "delete").decision, GateDecision.ESCALATE)
            runtime.approve(contract, "delete", "kbssk", "Approved specific delete")
            self.assertEqual(runtime.can_execute_action(contract, "delete").decision, GateDecision.PASS)

    def test_check_tool_allowed_enforces_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = OuroborosHITL(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(
                GoalContract(
                    "Scoped",
                    "limited tools",
                    Risk.LOW,
                    permissions=PermissionContract(allowed_tools=("Edit", "Bash")),
                )
            )

            self.assertEqual(runtime.check_tool_allowed(contract, "Edit").decision, GateDecision.PASS)
            self.assertEqual(runtime.check_tool_allowed(contract, "Curl").decision, GateDecision.ESCALATE)

    def test_check_tool_allowed_passes_when_unrestricted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = OuroborosHITL(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(GoalContract("Open", "no tool limits"))

            self.assertEqual(runtime.check_tool_allowed(contract, "anything").decision, GateDecision.PASS)

    def test_check_non_goal_stops_on_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = OuroborosHITL(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(
                GoalContract("Slice", "thin", non_goals=("touch CI config",))
            )

            self.assertEqual(
                runtime.check_non_goal(contract, "now I will touch CI config").decision,
                GateDecision.STOP,
            )
            self.assertEqual(
                runtime.check_non_goal(contract, "edit contracts.py").decision,
                GateDecision.PASS,
            )

    def test_should_stop_reads_stopping_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = OuroborosHITL(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(
                GoalContract(
                    "Loop",
                    "bounded",
                    stopping_policy={
                        "max_iterations": 3,
                        "no_progress_iterations": 2,
                        "max_failed_hypotheses": 3,
                    },
                )
            )

            self.assertEqual(runtime.should_stop(contract, {"iterations": 1}).decision, GateDecision.PASS)
            self.assertEqual(runtime.should_stop(contract, {"iterations": 3}).decision, GateDecision.STOP)
            self.assertEqual(
                runtime.should_stop(contract, {"no_progress_iterations": 2}).decision,
                GateDecision.STOP,
            )
            self.assertEqual(
                runtime.should_stop(contract, {"failed_hypotheses": 3}).decision,
                GateDecision.ESCALATE,
            )


if __name__ == "__main__":
    unittest.main()
