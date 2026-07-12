from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import (
    AuditEventType,
    EvidenceKind,
    EvidenceRequirement,
    GateDecision,
    GoalContract,
    PermissionContract,
    Risk,
    VerifierDecision,
)
from causality.orchestrator import Causality


class GateTests(unittest.TestCase):
    def test_low_risk_completion_requires_evidence_and_two_verifiers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
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

    def test_hollow_verifier_pass_does_not_count(self) -> None:
        # P2: a "pass" with neither rationale nor evidence is a hollow
        # rubber-stamp; two of them must not satisfy the independent-pass quorum.
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(GoalContract("Hollow", "no evidence required"))
            runtime.record_verifier(contract, VerifierDecision("v1", "pass", ""))
            runtime.record_verifier(contract, VerifierDecision("v2", "pass", "   "))
            result = runtime.complete(contract)
            self.assertEqual(result.decision, GateDecision.REPAIR)
            self.assertTrue(any("unsubstantiated" in reason for reason in result.reasons))
            # A rationale-backed pass and an evidence-cited pass complete it.
            runtime.record_verifier(
                contract, VerifierDecision("v1", "pass", "ran the suite, all green")
            )
            runtime.record_verifier(
                contract, VerifierDecision("v2", "pass", "", evidence_refs=("ev-1",))
            )
            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_blank_evidence_ref_is_not_substantive(self) -> None:
        # codex r3447999380: a placeholder evidence_refs=("",) with no rationale
        # is not a real citation -- it must not count, even under require_evidence.
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(GoalContract("Blank", "blank refs"))
            runtime.record_verifier(contract, VerifierDecision("v1", "pass", "", evidence_refs=("",)))
            runtime.record_verifier(contract, VerifierDecision("v2", "pass", "", evidence_refs=("  ",)))
            self.assertEqual(runtime.complete(contract).decision, GateDecision.REPAIR)
            self.assertEqual(
                runtime.gate.complete(contract, require_evidence=True).decision,
                GateDecision.REPAIR,
            )

    def test_require_evidence_rejects_rationale_only_passes(self) -> None:
        # P2: the strict bar demands an evidence_ref -- prose alone no longer counts.
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(GoalContract("Strict", "high assurance"))
            runtime.record_verifier(contract, VerifierDecision("v1", "pass", "looks right"))
            runtime.record_verifier(contract, VerifierDecision("v2", "pass", "also fine"))
            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)
            self.assertEqual(
                runtime.gate.complete(contract, require_evidence=True).decision,
                GateDecision.REPAIR,
            )
            runtime.record_verifier(
                contract, VerifierDecision("v1", "pass", "ran suite", evidence_refs=("ev-1",))
            )
            runtime.record_verifier(
                contract, VerifierDecision("v2", "pass", "diffed", evidence_refs=("ev-2",))
            )
            self.assertEqual(
                runtime.gate.complete(contract, require_evidence=True).decision,
                GateDecision.PASS,
            )

    def test_high_risk_plan_and_final_require_human_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
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

            runtime.approve(contract, "final", "kbssk", "Too early")
            evidence = runtime.record_evidence(
                contract,
                EvidenceKind.TEST_OUTPUT,
                {"output": "passed"},
            )
            refs = (evidence.entry_hash,)
            runtime.record_verifier(
                contract,
                VerifierDecision(
                    "correctness",
                    "pass",
                    "tests passed",
                    evidence_refs=refs,
                ),
            )
            runtime.record_verifier(
                contract,
                VerifierDecision(
                    "evidence",
                    "pass",
                    "evidence present",
                    evidence_refs=refs,
                ),
            )
            self.assertEqual(runtime.complete(contract).decision, GateDecision.ESCALATE)

            runtime.approve(
                contract,
                "final",
                "kbssk",
                "Raw evidence reviewed",
                evidence_refs=refs,
            )
            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

            runtime.reject(contract, "final", "kbssk", "Approval withdrawn")
            self.assertEqual(runtime.complete(contract).decision, GateDecision.ESCALATE)

    def test_public_decisions_cannot_precede_durable_contract_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            contract = GoalContract("Deploy", "not yet bound", risk=Risk.HIGH)

            with self.assertRaises(ValueError):
                runtime.approve(contract, "plan", "kbssk", "premature")
            with self.assertRaises(ValueError):
                runtime.record_verifier(
                    contract,
                    VerifierDecision("premature", "pass", "not bound"),
                )
            with self.assertRaises(ValueError):
                runtime.transition(contract, "blocked", "not bound")

            runtime.create_contract(contract)
            self.assertEqual(
                runtime.evaluate_plan(contract).decision,
                GateDecision.ESCALATE,
            )

    def test_irreversible_action_requires_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(GoalContract("Delete data", "Dangerous", Risk.LOW))

            self.assertEqual(runtime.can_execute_action(contract, "delete").decision, GateDecision.ESCALATE)
            runtime.approve(contract, "delete", "kbssk", "Approved specific delete")
            self.assertEqual(runtime.can_execute_action(contract, "delete").decision, GateDecision.PASS)

    def test_check_tool_allowed_enforces_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
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
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(GoalContract("Open", "no tool limits"))

            self.assertEqual(runtime.check_tool_allowed(contract, "anything").decision, GateDecision.PASS)

    def test_public_clause_gates_reject_live_contract_relaxation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            tool_contract = runtime.create_contract(
                GoalContract(
                    "Tool",
                    "frozen allowlist",
                    permissions=PermissionContract(allowed_tools=("git",)),
                )
            )
            non_goal_contract = runtime.create_contract(
                GoalContract("Scope", "frozen boundary", non_goals=("forbidden",))
            )
            stop_contract = runtime.create_contract(
                GoalContract("Loop", "frozen limit", stopping_policy={"max_iterations": 1})
            )

            tool_contract.permissions = PermissionContract()
            non_goal_contract.non_goals = ()
            stop_contract.stopping_policy = {"max_iterations": 999}

            for result in (
                runtime.check_tool_allowed(tool_contract, "shell"),
                runtime.check_non_goal(non_goal_contract, "forbidden operation"),
                runtime.should_stop(stop_contract, {"iterations": 1}),
            ):
                with self.subTest(result=result):
                    self.assertEqual(result.decision, GateDecision.REPAIR)
                    self.assertIn(
                        "live contract differs from durable contract snapshot",
                        result.reasons,
                    )

    def test_check_non_goal_stops_on_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
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
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
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

    def test_should_stop_continue_poll_records_no_gate_decision(self) -> None:
        # Observer effect: the loop polls should_stop before every
        # iteration, so a non-terminal "keep going" result must NOT append a
        # GATE_DECISION. Recording each poll would flood the ledger with one
        # event per iteration and inflate Reflect's gate_counts[pass]. Only a
        # terminal STOP/ESCALATE is a material decision worth recording.
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(
                GoalContract(
                    "Loop",
                    "bounded",
                    stopping_policy={"max_iterations": 3, "max_failed_hypotheses": 2},
                )
            )

            def gate_decisions() -> list:
                return [
                    e
                    for e in runtime.ledger.events()
                    if e.event_type == AuditEventType.GATE_DECISION.value
                    and e.contract_id == contract.goal_id
                ]

            # Many "keep going" polls leave no footprint in the ledger.
            for _ in range(5):
                self.assertEqual(
                    runtime.should_stop(contract, {"iterations": 1}).decision,
                    GateDecision.PASS,
                )
            self.assertEqual(gate_decisions(), [])

            # A terminal STOP is recorded exactly once.
            self.assertEqual(
                runtime.should_stop(contract, {"iterations": 3}).decision,
                GateDecision.STOP,
            )
            recorded = gate_decisions()
            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0].payload.get("decision"), GateDecision.STOP.value)

            # A terminal ESCALATE (failed hypotheses exhausted) is also recorded.
            self.assertEqual(
                runtime.should_stop(contract, {"failed_hypotheses": 2}).decision,
                GateDecision.ESCALATE,
            )
            self.assertEqual(len(gate_decisions()), 2)

    def test_same_verifier_twice_is_not_two_independent_passes(self) -> None:
        # Regression F1: counting raw events let one verifier passing across two
        # loop iterations satisfy the >=2-pass rule.
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(
                GoalContract(
                    "Repeat",
                    "single verifier twice",
                    evidence_required=[EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "tests")],
                )
            )
            runtime.record_evidence(contract, EvidenceKind.TEST_OUTPUT, {"output": "ok"})
            runtime.record_verifier(contract, VerifierDecision("correctness", "pass", "round 1"))
            runtime.record_verifier(contract, VerifierDecision("correctness", "pass", "round 2"))

            # One verifier, two events -> still only one independent pass.
            self.assertEqual(runtime.complete(contract).decision, GateDecision.REPAIR)

            runtime.record_verifier(contract, VerifierDecision("evidence", "pass", "second verifier"))
            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_fixed_critical_failure_no_longer_blocks_completion(self) -> None:
        # Regression F2: a critical fail from an earlier round must not block
        # forever once that verifier later passes.
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(
                GoalContract(
                    "Heal",
                    "critical then fixed",
                    evidence_required=[EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "tests")],
                )
            )
            runtime.record_evidence(contract, EvidenceKind.TEST_OUTPUT, {"output": "ok"})
            runtime.record_verifier(
                contract, VerifierDecision("safety", "fail", "unsafe", severity="critical")
            )
            self.assertEqual(runtime.complete(contract).decision, GateDecision.REPAIR)

            # The same verifier now passes; a second independent verifier passes.
            runtime.record_verifier(contract, VerifierDecision("safety", "pass", "now safe"))
            runtime.record_verifier(contract, VerifierDecision("evidence", "pass", "evidence present"))
            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_complete_with_empty_list_does_not_fall_back_to_ledger(self) -> None:
        # Regression H1: an explicit empty decision list is `is None`-distinct
        # from "not supplied" and must not silently re-read ledger history.
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(GoalContract("Empty", "no decisions"))
            runtime.record_verifier(contract, VerifierDecision("a", "pass", "x"))
            runtime.record_verifier(contract, VerifierDecision("b", "pass", "y"))

            # Caller explicitly judges with no decisions -> REPAIR, not PASS.
            self.assertEqual(runtime.gate.complete(contract, []).decision, GateDecision.REPAIR)


if __name__ == "__main__":
    unittest.main()
