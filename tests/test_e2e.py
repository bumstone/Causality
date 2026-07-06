"""End-to-end evolution-loop integration tests.

The per-module tests cover layers in isolation; these exercise the *whole*
self-improvement loop across MULTIPLE runs, which is the only way to prove the
"loop is closed" claim end to end (June 2026 P3 review):

- back half: a passing run distills an earned skill, reproducibility is accrued,
  a HITL gate promotes it, and a *later related* run recalls and injects it;
- guardrail feedback: a failing run records a scoped failure that a *later* run
  in the same scope feeds forward as a confirmed non_goal;
- the bounded loop genuinely iterates (review fails, then passes) before
  completing.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import AuditEventType, EvidenceKind, GateDecision, GoalContract, VerifierDecision
from causality.engine import CausalityEngine
from causality.tool_adapter import ToolAdapter


def _passing_verifiers():
    return [
        lambda c: VerifierDecision("correctness", "pass", "looks right"),
        lambda c: VerifierDecision("evidence", "pass", "evidence present"),
    ]


class E2ELoopTests(unittest.TestCase):
    def _engine(self, temp_dir: str) -> CausalityEngine:
        return CausalityEngine(Path(temp_dir))

    def _work(self, engine: CausalityEngine):
        def work(contract: GoalContract, iteration: int) -> None:
            engine.runtime.record_evidence(contract, EvidenceKind.TEST_OUTPUT, {"output": "ok"})
        return work

    def test_tool_adapter_inside_a_full_run(self) -> None:
        # The bundled ToolAdapter works through a real run: its gated file write
        # actually happens and is recorded, and the run still completes (closes
        # the "tool adapter inside a full run" E2E gap).
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            root = Path(temp_dir)

            def work(contract: GoalContract, iteration: int, adapter) -> None:
                tools = ToolAdapter(engine.runtime.ledger, adapter, root=root)
                tools.write_text("out/result.txt", "done")  # gated, recorded
                engine.runtime.record_evidence(contract, EvidenceKind.TEST_OUTPUT, {"output": "ok"})

            run = engine.run_task(
                objective="implement the report writer",
                work=work,
                verifiers=_passing_verifiers(),
                verification=["python -m unittest"],
                stop_condition={"max_iterations": 3},
                allowed_tools=["file.write"],  # the tool the adapter routes through
            )

            self.assertTrue(run.passed)
            self.assertEqual(run.loop.decision, GateDecision.PASS)
            self.assertTrue((root / "out" / "result.txt").exists())
            evidence = engine.runtime.ledger.find(AuditEventType.EVIDENCE)
            self.assertTrue(any(e.payload.get("tool") == "file.write" for e in evidence))

    def test_tool_adapter_blocked_action_terminates_run(self) -> None:
        # A gated tool call that breaches the contract (tool not allowed) raises
        # ActionBlocked inside work, and the engine terminates the run with the
        # gate's decision rather than completing.
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            root = Path(temp_dir)

            def work(contract: GoalContract, iteration: int, adapter) -> None:
                tools = ToolAdapter(engine.runtime.ledger, adapter, root=root)
                tools.write_text("out/x.txt", "data", tool="file.write")  # not allowed below

            run = engine.run_task(
                objective="implement the writer",
                work=work,
                verifiers=_passing_verifiers(),
                verification=["python -m unittest"],
                stop_condition={"max_iterations": 3},
                allowed_tools=["Edit"],  # "file.write" is outside scope -> ESCALATE
            )

            self.assertFalse(run.passed)
            self.assertEqual(run.loop.decision, GateDecision.ESCALATE)
            self.assertFalse((root / "out" / "x.txt").exists())

    def test_back_half_loop_distill_promote_recall_reuse(self) -> None:
        # The full earned-skill read-path across runs: distill -> reproduce ->
        # HITL promote -> recall+inject into a later related run.
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = self._engine(temp_dir)

            run1 = engine.run_task(
                objective="implement the login parser",
                work=self._work(engine),
                verifiers=_passing_verifiers(),
                verification=["python -m unittest"],
                stop_condition={"max_iterations": 3},
            )
            self.assertTrue(run1.passed)
            self.assertIsNotNone(run1.skill)
            skill_id = run1.skill.skill_id
            # A passing run with no promoted library yet recalls nothing.
            self.assertEqual(run1.recalled_skills, ())

            # Accrue n-of-m reproducibility (2 of 3), then HITL-promote.
            engine.skills.record_outcome(skill_id, success=True)
            engine.skills.record_outcome(skill_id, success=True)
            engine.skills.record_outcome(skill_id, success=False)
            promoted = engine.skills.promote(
                skill_id, approved_by="alice", authored_names=("tdd",)
            )
            self.assertEqual(promoted.skill_id, skill_id)

            # A later RELATED objective recalls and injects the promoted skill.
            run2 = engine.run_task(
                objective="implement the login validator",
                work=self._work(engine),
                verifiers=_passing_verifiers(),
                verification=["python -m unittest"],
                stop_condition={"max_iterations": 3},
            )
            self.assertTrue(run2.passed)
            recalled_ids = [s.skill_id for s in run2.recalled_skills]
            self.assertIn(skill_id, recalled_ids)

            # An UNRELATED objective shares no content tokens -> not recalled.
            run3 = engine.run_task(
                objective="publish the quarterly finance report",
                work=self._work(engine),
                verifiers=_passing_verifiers(),
                verification=["python -m unittest"],
                stop_condition={"max_iterations": 3},
            )
            self.assertNotIn(skill_id, [s.skill_id for s in run3.recalled_skills])

    def test_failure_feeds_forward_as_guardrail_across_runs(self) -> None:
        # A scoped failure recorded by one run is offered to a later run in the
        # same scope, and the human-confirmed clause becomes a bound non_goal.
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = self._engine(temp_dir)
            failing = [lambda c: VerifierDecision("correctness", "fail", "broke billing")]

            run1 = engine.run_task(
                objective="rework the billing retry logic",
                work=self._work(engine),
                verifiers=failing,
                verification=["python -m unittest"],
                stop_condition={"max_iterations": 2, "no_progress_iterations": 99},
                failure_scope="billing",
            )
            self.assertFalse(run1.passed)
            self.assertGreaterEqual(len(engine.memory.entries("failures")), 1)

            guardrail = "never retry a charge without idempotency key"
            confirmed: list = []

            def confirm(active_failures):
                # The active scoped failure is offered for human curation.
                confirmed.extend(active_failures)
                return [guardrail]

            run2 = engine.run_task(
                objective="add a billing dashboard widget",
                work=self._work(engine),
                verifiers=_passing_verifiers(),
                verification=["python -m unittest"],
                stop_condition={"max_iterations": 3},
                failure_scope="billing",
                confirm_guardrails=confirm,
            )
            # The prior failure was surfaced and the confirmed clause is bound.
            self.assertTrue(confirmed)
            self.assertIn(guardrail, run2.task.non_goals)

    def test_bounded_loop_iterates_then_completes(self) -> None:
        # The loop genuinely iterates: review fails the first pass, then passes.
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = self._engine(temp_dir)
            state = {"iteration": 0}

            def work(contract: GoalContract, iteration: int) -> None:
                state["iteration"] = iteration
                engine.runtime.record_evidence(
                    contract, EvidenceKind.TEST_OUTPUT, {"output": "ok"}
                )

            def correctness(contract):
                ok = state["iteration"] >= 2  # passes only from the 2nd iteration
                return VerifierDecision(
                    "correctness", "pass" if ok else "fail", "ready" if ok else "not yet"
                )

            run = engine.run_task(
                objective="implement the retry backoff",
                work=work,
                verifiers=[correctness, lambda c: VerifierDecision("evidence", "pass", "present")],
                verification=["python -m unittest"],
                stop_condition={"max_iterations": 5, "no_progress_iterations": 5},
            )
            self.assertTrue(run.passed)
            self.assertEqual(run.loop.decision, GateDecision.PASS)
            self.assertGreaterEqual(run.loop.iterations, 2)


if __name__ == "__main__":
    unittest.main()
