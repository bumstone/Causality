from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.agent_harness import AgentHarness, Dispatch, TaskType


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = AgentHarness()

    def test_planning_routes_to_gstack_bundle(self) -> None:
        dispatch = self.harness.route(TaskType.PLANNING)
        self.assertEqual(dispatch.task_type, TaskType.PLANNING)
        self.assertEqual(dispatch.architecture, "gstack")
        self.assertEqual(dispatch.playbook, ("office-hours", "ceo-review"))

    def test_implementation_routes_to_superpowers_bundle(self) -> None:
        dispatch = self.harness.route(TaskType.IMPLEMENTATION)
        self.assertEqual(dispatch.architecture, "superpowers")
        self.assertEqual(dispatch.playbook, ("tdd", "debugging"))

    def test_long_running_routes_to_causality_bundle(self) -> None:
        dispatch = self.harness.route(TaskType.LONG_RUNNING)
        self.assertEqual(dispatch.architecture, "causality")
        self.assertEqual(dispatch.playbook, ("contract-harness", "limited-causality-loop"))

    def test_release_routes_to_gstack_bundle(self) -> None:
        dispatch = self.harness.route(TaskType.RELEASE)
        self.assertEqual(dispatch.architecture, "gstack")
        self.assertEqual(dispatch.playbook, ("ship", "qa-checklist"))

    def test_trivial_routes_to_no_playbook(self) -> None:
        dispatch = self.harness.route(TaskType.TRIVIAL)
        self.assertEqual(dispatch.architecture, "")
        self.assertEqual(dispatch.playbook, ())

    def test_route_accepts_string_form(self) -> None:
        from_enum = self.harness.route(TaskType.IMPLEMENTATION)
        from_str = self.harness.route("implementation")
        self.assertEqual(from_enum, from_str)
        self.assertEqual(from_str.architecture, "superpowers")

    def test_route_string_form_for_each_type(self) -> None:
        for task_type in TaskType:
            with self.subTest(task_type=task_type):
                self.assertEqual(
                    self.harness.route(task_type.value),
                    self.harness.route(task_type),
                )

    def test_never_blends_bundles(self) -> None:
        # Exactly one bundle per type; no type shares another type's bundle.
        bundles = {tt: self.harness.route(tt).playbook for tt in TaskType}
        non_trivial = [b for tt, b in bundles.items() if tt is not TaskType.TRIVIAL]
        self.assertEqual(len(non_trivial), len(set(non_trivial)))

    def test_route_raises_on_unknown_string(self) -> None:
        with self.assertRaises(ValueError):
            self.harness.route("megablend")

    def test_route_raises_on_unknown_type(self) -> None:
        with self.assertRaises(ValueError):
            self.harness.route(42)  # type: ignore[arg-type]


class ClassifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = AgentHarness()

    def test_planning_phrases(self) -> None:
        for phrase in ("Let's plan the Q3 roadmap", "design the schema", "brainstorm ideas"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.harness.classify(phrase), TaskType.PLANNING)

    def test_implementation_phrases(self) -> None:
        for phrase in ("implement the parser", "fix this bug", "refactor the module", "add a test"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.harness.classify(phrase), TaskType.IMPLEMENTATION)

    def test_release_phrases(self) -> None:
        for phrase in ("release v1.2", "ship it", "deploy to prod", "publish the package"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.harness.classify(phrase), TaskType.RELEASE)

    def test_long_running_phrases(self) -> None:
        for phrase in ("run an autonomous overnight job", "unattended long-running migration"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.harness.classify(phrase), TaskType.LONG_RUNNING)

    def test_long_running_takes_precedence_over_implementation(self) -> None:
        # "autonomous ... refactor" must not fall through to IMPLEMENTATION.
        self.assertEqual(
            self.harness.classify("autonomous overnight refactor"),
            TaskType.LONG_RUNNING,
        )

    def test_case_insensitive(self) -> None:
        self.assertEqual(self.harness.classify("IMPLEMENT the FEATURE"), TaskType.IMPLEMENTATION)

    def test_trivial_fallback(self) -> None:
        for phrase in ("what time is it?", "say hello", ""):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.harness.classify(phrase), TaskType.TRIVIAL)

    def test_classify_then_route_end_to_end(self) -> None:
        dispatch = self.harness.route(self.harness.classify("implement the feature"))
        self.assertEqual(dispatch.architecture, "superpowers")


class DispatchTests(unittest.TestCase):
    def test_to_dict_round_trips_fields(self) -> None:
        dispatch = Dispatch(
            task_type=TaskType.PLANNING,
            architecture="gstack",
            playbook=("office-hours", "ceo-review"),
        )
        self.assertEqual(
            dispatch.to_dict(),
            {
                "task_type": "planning",
                "architecture": "gstack",
                "playbook": ["office-hours", "ceo-review"],
            },
        )

    def test_to_dict_trivial(self) -> None:
        dispatch = AgentHarness().route(TaskType.TRIVIAL)
        self.assertEqual(
            dispatch.to_dict(),
            {"task_type": "trivial", "architecture": "", "playbook": []},
        )


if __name__ == "__main__":
    unittest.main()
