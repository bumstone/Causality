from __future__ import annotations

import sys
import tempfile
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import (
    AuditEventType,
    EvidenceKind,
    GateDecision,
    GoalContract,
    PermissionContract,
    Risk,
    VerificationRequirement,
    VerifierDecision,
)
from causality.engine import CausalityEngine, _accepts_adapter
from causality.execution import ActionBlocked, ExecutionAdapter, PlanApproval


def _verification() -> tuple[VerificationRequirement, ...]:
    return (
        VerificationRequirement(
            id="unit",
            argv=(sys.executable, "-c", "raise SystemExit(0)"),
        ),
    )


def _result_hash(engine: CausalityEngine, contract) -> str:
    return [
        event.entry_hash
        for event in engine.runtime.ledger.events_for_contract(contract.goal_id)
        if event.event_type == AuditEventType.EVIDENCE.value
        and event.payload.get("kind") == "verification_result"
    ][-1]


def _passing_verifiers(engine: CausalityEngine):

    return [
        lambda c: VerifierDecision(
            "correctness", "pass", "looks right", evidence_refs=(_result_hash(engine, c),)
        ),
        lambda c: VerifierDecision(
            "evidence", "pass", "evidence present", evidence_refs=(_result_hash(engine, c),)
        ),
    ]


class PlanApprovalTests(unittest.TestCase):
    def test_requires_nonblank_approver_and_rationale_at_construction(self) -> None:
        with self.assertRaises(TypeError):
            PlanApproval("alice")  # type: ignore[call-arg]
        for approval in (("", "release sign-off"), ("alice", "")):
            with self.subTest(approval=approval), self.assertRaises(ValueError):
                PlanApproval(*approval)


class AdapterUnitTests(unittest.TestCase):
    """ExecutionAdapter enforces the contract's per-action gates."""

    def _bound(self, engine: CausalityEngine, **overrides):
        params = dict(
            objective="do the thing",
            verification=_verification(),
            stop_condition={"max_iterations": 3},
            non_goals=["delete production data"],
            allowed_tools=["Bash", "Edit"],
            risk=Risk.LOW,
        )
        params.update(overrides)
        return engine.harness.bind(**params)

    def test_in_scope_action_runs_and_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            adapter = ExecutionAdapter(engine.runtime, self._bound(engine).contract)
            ran = []
            out = adapter.execute(
                tool="Bash",
                action_kind="click",
                description="run the unit tests",
                run=lambda: ran.append("x") or "done",
            )
            self.assertEqual(out, "done")
            self.assertEqual(ran, ["x"])

    def test_non_goal_breach_blocks_with_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            adapter = ExecutionAdapter(engine.runtime, self._bound(engine).contract)
            ran = []
            with self.assertRaises(ActionBlocked) as ctx:
                adapter.execute(
                    tool="Bash",
                    action_kind="click",
                    description="delete production data now",
                    run=lambda: ran.append("x"),
                )
            self.assertEqual(ctx.exception.result.decision, GateDecision.STOP)
            self.assertEqual(ran, [])  # refused action never executed

    def test_out_of_scope_tool_blocks_with_escalate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            adapter = ExecutionAdapter(engine.runtime, self._bound(engine).contract)
            with self.assertRaises(ActionBlocked) as ctx:
                adapter.execute(
                    tool="Browser",
                    action_kind="click",
                    description="open the page",
                    run=lambda: None,
                )
            self.assertEqual(ctx.exception.result.decision, GateDecision.ESCALATE)
            self.assertEqual(ctx.exception.tool, "Browser")

    def test_adapter_keeps_durable_permissions_after_live_object_widens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            bound = self._bound(engine, allowed_tools=["Bash"])
            adapter = ExecutionAdapter(engine.runtime, bound.contract)
            bound.contract.permissions = PermissionContract()
            ran: list[str] = []

            with self.assertRaises(ActionBlocked):
                adapter.execute(
                    tool="danger",
                    action_kind="click",
                    description="unauthorized action",
                    run=lambda: ran.append("ran"),
                )

            self.assertEqual(ran, [])

    def test_irreversible_action_without_approval_blocks_with_escalate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            adapter = ExecutionAdapter(engine.runtime, self._bound(engine).contract)
            with self.assertRaises(ActionBlocked) as ctx:
                adapter.execute(
                    tool="Bash",
                    action_kind="delete",
                    description="remove the temp file",
                    run=lambda: None,
                )
            self.assertEqual(ctx.exception.result.decision, GateDecision.ESCALATE)

    def test_first_refusal_short_circuits_remaining_gates(self) -> None:
        # A non-goal breach (the first gate) must not also record a tool PASS:
        # exactly one GATE_DECISION (the STOP) should be appended.
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            adapter = ExecutionAdapter(engine.runtime, self._bound(engine).contract)
            before = len(engine.runtime.ledger.find(AuditEventType.GATE_DECISION))
            with self.assertRaises(ActionBlocked):
                adapter.execute(
                    tool="Browser",  # also out-of-scope, but should never be checked
                    action_kind="delete",
                    description="delete production data",
                    run=lambda: None,
                )
            after = engine.runtime.ledger.find(AuditEventType.GATE_DECISION)
            self.assertEqual(len(after) - before, 1)
            self.assertEqual(after[-1].payload.get("decision"), GateDecision.STOP.value)

    def test_anonymous_network_action_records_auth_gate_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            contract = engine.runtime.create_contract(
                GoalContract(
                    "anonymous HTTP",
                    "record every scope gate",
                    permissions=PermissionContract(
                        allowed_tools=("http",),
                        network_scope=("https://api.example",),
                    ),
                )
            )
            adapter = ExecutionAdapter(engine.runtime, contract)

            adapter.execute(
                tool="http",
                action_kind="tool_call",
                description="anonymous request",
                network_origin="https://api.example",
                auth_ref=None,
                run=lambda: None,
            )

            reasons = [
                " ".join(event.payload.get("reasons", []))
                for event in engine.runtime.ledger.find(AuditEventType.GATE_DECISION)
            ]
            self.assertTrue(any("anonymous access is permitted" in item for item in reasons))


class EngineGatingTests(unittest.TestCase):
    """run_task wires the plan gate and per-action gates into the loop."""

    def _gated_work(self, engine: CausalityEngine, *, description: str, tool: str = "Bash", kind: str = "click"):
        def work(contract, iteration, adapter):
            adapter.execute(
                tool=tool,
                action_kind=kind,
                description=description,
                run=lambda: engine.runtime.record_evidence(
                    contract, EvidenceKind.TEST_OUTPUT, {"output": "ok"}
                ),
            )

        return work

    def test_gated_work_in_scope_passes_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            run = engine.run_task(
                objective="implement the parser",
                work=self._gated_work(engine, description="run the unit tests"),
                verifiers=_passing_verifiers(engine),
                verification=_verification(),
                stop_condition={"max_iterations": 3},
                allowed_tools=["Bash", "shell"],
                non_goals=["delete production data"],
            )
            self.assertTrue(run.passed)
            self.assertEqual(run.loop.decision, GateDecision.PASS)

    def test_non_goal_action_stops_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            run = engine.run_task(
                objective="implement the parser",
                work=self._gated_work(engine, description="delete production data"),
                verifiers=_passing_verifiers(engine),
                verification=_verification(),
                stop_condition={"max_iterations": 3},
                allowed_tools=["Bash"],
                non_goals=["delete production data"],
            )
            self.assertFalse(run.passed)
            self.assertEqual(run.loop.decision, GateDecision.STOP)
            self.assertIsNone(run.skill)
            # The blocked action ran no work, so no evidence was recorded and the
            # review never ran.
            evidence = [
                e
                for e in engine.runtime.ledger.find(AuditEventType.EVIDENCE)
                if e.contract_id == run.task.goal_id
            ]
            self.assertEqual(evidence, [])
            self.assertIsNone(run.review)

    def test_out_of_scope_tool_escalates_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            run = engine.run_task(
                objective="implement the parser",
                work=self._gated_work(engine, description="open a page", tool="Browser"),
                verifiers=_passing_verifiers(engine),
                verification=_verification(),
                stop_condition={"max_iterations": 3},
                allowed_tools=["Bash"],
            )
            self.assertFalse(run.passed)
            self.assertEqual(run.loop.decision, GateDecision.ESCALATE)
            self.assertIsNone(run.skill)

    def test_high_risk_plan_gate_blocks_before_any_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            calls: list[int] = []

            def work(contract, iteration, adapter):
                calls.append(iteration)

            run = engine.run_task(
                objective="deploy the release to production",
                work=work,
                verifiers=_passing_verifiers(engine),
                verification=_verification(),
                stop_condition={"max_iterations": 3},
                risk=Risk.IRREVERSIBLE,
            )
            self.assertFalse(run.passed)
            self.assertEqual(run.loop.decision, GateDecision.ESCALATE)
            self.assertEqual(calls, [])  # work never ran
            self.assertIsNone(run.review)
            self.assertIsNone(run.skill)
            # Reflect still captured the refused plan.
            self.assertEqual(len(engine.memory.entries("retrospectives")), 1)

    def test_approve_plan_hook_clears_plan_gate_and_runs_work(self) -> None:
        # With an approve_plan hook, an approval-required (high-risk) plan records
        # its plan-stage HUMAN_DECISION on the freshly bound contract, so the plan
        # gate clears and work actually runs (before this fix it short-circuited
        # before work could ever execute, even with human approval).
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            calls: list[int] = []

            def work(contract, iteration, adapter):
                calls.append(iteration)
                adapter.execute(
                    tool="Bash",
                    action_kind="click",
                    description="run the unit tests",
                    run=lambda: engine.runtime.record_evidence(
                        contract, EvidenceKind.TEST_OUTPUT, {"output": "ok"}
                    ),
                )

            run = engine.run_task(
                objective="ship the high-risk change",
                work=work,
                verifiers=_passing_verifiers(engine),
                verification=_verification(),
                stop_condition={"max_iterations": 3},
                allowed_tools=["Bash", "shell"],
                risk=Risk.HIGH,
                approve_plan=lambda c: PlanApproval("alice", "release sign-off"),
            )
            self.assertTrue(calls)  # work ran past the plan gate
            self.assertIsNotNone(run.review)
            evidence = [
                e
                for e in engine.runtime.ledger.find(AuditEventType.EVIDENCE)
                if e.contract_id == run.task.goal_id
            ]
            self.assertTrue(evidence)

    def test_approve_plan_hook_declining_escalates_before_work(self) -> None:
        # A hook that returns None declines: the plan still ESCALATEs, work never
        # runs -- the same terminal state as supplying no hook at all.
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            calls: list[int] = []

            def work(contract, iteration, adapter):
                calls.append(iteration)

            run = engine.run_task(
                objective="ship the high-risk change",
                work=work,
                verifiers=_passing_verifiers(engine),
                verification=_verification(),
                stop_condition={"max_iterations": 3},
                risk=Risk.HIGH,
                approve_plan=lambda c: None,
            )
            self.assertEqual(run.loop.decision, GateDecision.ESCALATE)
            self.assertEqual(calls, [])
            self.assertIsNone(run.review)

    def test_two_arg_work_runs_ungated_backcompat(self) -> None:
        # A legacy two-arg work that would breach a non-goal is NOT gated (it
        # never opted into the adapter), so the run is not stopped by the gate.
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))

            def work(contract, iteration):
                engine.runtime.record_evidence(contract, EvidenceKind.TEST_OUTPUT, {"output": "ok"})

            with self.assertWarns(DeprecationWarning):
                run = engine.run_task(
                    objective="implement the parser",
                    work=work,
                    verifiers=_passing_verifiers(engine),
                    verification=_verification(),
                    stop_condition={"max_iterations": 3},
                    non_goals=["delete production data"],
                )
            self.assertTrue(run.passed)


class AcceptsAdapterTests(unittest.TestCase):
    def test_arity_detection(self) -> None:
        self.assertFalse(_accepts_adapter(lambda c, i: None))
        self.assertTrue(_accepts_adapter(lambda c, i, a: None))
        self.assertTrue(_accepts_adapter(lambda *args: None))

    def test_bound_method_drops_self(self) -> None:
        class Worker:
            def run(self, contract, iteration, adapter):
                return None

        self.assertTrue(_accepts_adapter(Worker().run))

    def test_two_argument_work_emits_deprecation_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))

            def work(contract, iteration):
                engine.runtime.record_evidence(
                    contract, EvidenceKind.TEST_OUTPUT, {"output": "ok"}
                )

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                run = engine.run_task(
                    objective="implement the parser",
                    work=work,
                    verifiers=_passing_verifiers(engine),
                    verification=_verification(),
                    stop_condition={"max_iterations": 1},
                )

            self.assertTrue(run.passed)
            self.assertTrue(
                any(item.category is DeprecationWarning for item in caught),
                "two-argument work callback did not emit DeprecationWarning",
            )


if __name__ == "__main__":
    unittest.main()
