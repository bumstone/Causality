from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import EvidenceKind, GateDecision, GoalContract, VerifierDecision
from causality.engine import CausalityEngine, TaskRun
from causality.agent_harness import TaskType


def _passing_verifiers():
    return [
        lambda c: VerifierDecision("correctness", "pass", "looks right"),
        lambda c: VerifierDecision("evidence", "pass", "evidence present"),
    ]


class EngineTests(unittest.TestCase):
    def _engine(self, temp_dir: str) -> CausalityEngine:
        return CausalityEngine(Path(temp_dir))

    def _work(self, engine: CausalityEngine):
        def work(contract: GoalContract, iteration: int) -> None:
            engine.runtime.record_evidence(contract, EvidenceKind.TEST_OUTPUT, {"output": "ok"})
        return work

    def test_run_task_end_to_end_passes_all_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = self._engine(temp_dir)

            run = engine.run_task(
                objective="implement the parser",
                work=self._work(engine),
                verifiers=_passing_verifiers(),
                verification=["python -m unittest"],
                stop_condition={"max_iterations": 3},
                allowed_tools=["Edit", "Bash"],
                non_goals=["delete production data"],
            )

            self.assertIsInstance(run, TaskRun)
            # L1 dispatch classified the objective -> implementation/superpowers.
            self.assertEqual(run.dispatch.task_type, TaskType.IMPLEMENTATION)
            self.assertEqual(run.dispatch.architecture, "superpowers")
            # The dispatch's bundle labels resolve to vendored playbooks on the run.
            self.assertEqual([p.name for p in run.playbooks], ["tdd", "debugging"])
            self.assertEqual(
                [p["name"] for p in run.to_dict()["playbooks"]], ["tdd", "debugging"]
            )
            # L2 contract clauses are frozen on the run.
            self.assertEqual(run.task.non_goals, ("delete production data",))
            self.assertEqual(run.task.allowed_tools, ("Edit", "Bash"))
            # L3 loop completed via automated review.
            self.assertTrue(run.passed)
            self.assertEqual(run.loop.decision, GateDecision.PASS)
            self.assertEqual(run.loop.iterations, 1)
            self.assertIsNotNone(run.review)
            self.assertTrue(run.review.approved)
            # L0 reflect distilled a retrospective into typed memory.
            self.assertEqual(len(engine.memory.entries("retrospectives")), 1)
            # Back half: an earned-skill candidate was distilled and persisted.
            self.assertIsNotNone(run.skill)
            self.assertTrue(run.skill.steps)
            self.assertEqual(len(engine.skills.candidates()), 1)

    def test_run_task_stops_when_review_never_approves(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = self._engine(temp_dir)
            failing = [lambda c: VerifierDecision("correctness", "fail", "nope")]

            run = engine.run_task(
                objective="implement the parser",
                work=self._work(engine),
                verifiers=failing,
                verification=["python -m unittest"],
                stop_condition={"max_iterations": 2, "no_progress_iterations": 99},
            )

            # Never reaches 2 passes -> completion never passes -> bounded by max_iterations.
            self.assertFalse(run.passed)
            self.assertEqual(run.loop.decision, GateDecision.STOP)
            self.assertIsNone(run.skill)  # no skill distilled on a non-pass
            # Reflect still runs and captures the failures.
            self.assertGreaterEqual(len(engine.memory.entries("failures")), 1)

    def test_run_next_pulls_from_agenda_and_completes_on_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = self._engine(temp_dir)
            self.assertIsNone(
                engine.run_next(
                    work=self._work(engine),
                    verifiers=_passing_verifiers(),
                    verification=["python -m unittest"],
                    stop_condition={"max_iterations": 3},
                )
            )

            item = engine.agenda.add("ship the release", priority=5)

            run = engine.run_next(
                work=self._work(engine),
                verifiers=_passing_verifiers(),
                verification=["python -m unittest"],
                stop_condition={"max_iterations": 3},
            )

            self.assertIsNotNone(run)
            self.assertEqual(run.dispatch.task_type, TaskType.RELEASE)
            self.assertTrue(run.passed)
            done = engine.agenda.items(status="done")
            self.assertEqual([i.item_id for i in done], [item.item_id])
            self.assertIsNone(engine.agenda.next_pending())

    def test_string_task_type_value_routes_correctly(self) -> None:
        # Regression F7: a TaskType *value* string must route by value, not be
        # keyword-classified ("long_running" misses keywords -> TRIVIAL).
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = self._engine(temp_dir)
            run = engine.run_task(
                objective="migrate the data store overnight",
                work=self._work(engine),
                verifiers=_passing_verifiers(),
                verification=["python -m unittest"],
                stop_condition={"max_iterations": 3},
                task_type="long_running",
            )
            self.assertEqual(run.dispatch.task_type, TaskType.LONG_RUNNING)
            self.assertEqual(run.dispatch.architecture, "causality")

    def test_failed_run_next_defers_item_back_to_pending(self) -> None:
        # Regression F10: a non-passing run must not strand the item "active".
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = self._engine(temp_dir)
            item = engine.agenda.add("implement the parser", priority=1)
            failing = [lambda c: VerifierDecision("correctness", "fail", "nope")]

            run = engine.run_next(
                work=self._work(engine),
                verifiers=failing,
                verification=["python -m unittest"],
                stop_condition={"max_iterations": 2, "no_progress_iterations": 99},
            )

            self.assertIsNotNone(run)
            self.assertFalse(run.passed)
            # Item is back in the queue, not stranded "active".
            self.assertEqual(engine.agenda.items(status="active"), [])
            nxt = engine.agenda.next_pending()
            self.assertIsNotNone(nxt)
            self.assertEqual(nxt.item_id, item.item_id)


if __name__ == "__main__":
    unittest.main()
