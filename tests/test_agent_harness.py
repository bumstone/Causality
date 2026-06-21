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

    def test_sensitive_unmatched_is_governed_not_trivial(self) -> None:
        # P2: risky work with no task keyword must not fall to TRIVIAL (which
        # bypasses the contract gates); it routes to a governed type instead.
        for phrase in (
            "clean up the payment module",
            "wipe the production database",
            "rotate the access token",
            "rotate the API key",  # spaced form must match too
            "rotate the ssh key",  # non-API key-rotation forms must match too
            "replace the private key",
            "change the access key",
            "revoke her access",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.harness.classify(phrase), TaskType.IMPLEMENTATION)

    def test_sensitive_gerund_forms_are_governed(self) -> None:
        # codex r3448006269: inflected risky verbs ("deleting", "revoking",
        # "charging", "wiping") must still route to a governed type, not TRIVIAL.
        for phrase in (
            "deleting customer records",
            "revoking her access",
            "charging the saved card",
            "wiping the cache",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.harness.classify(phrase), TaskType.IMPLEMENTATION)

    def test_benign_unmatched_stays_trivial(self) -> None:
        # Genuinely trivial, non-sensitive text is still answered directly.
        for phrase in ("say hello", "what is the weather", "summarize this note"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.harness.classify(phrase), TaskType.TRIVIAL)

    def test_default_can_govern_all_unmatched(self) -> None:
        # A strict caller can govern even benign unmatched text via default=.
        self.assertEqual(
            self.harness.classify("say hello", default=TaskType.IMPLEMENTATION),
            TaskType.IMPLEMENTATION,
        )

    def test_keyword_inside_unrelated_word_does_not_misroute(self) -> None:
        # codex review r3382219473: a keyword must not match inside an unrelated
        # word ("test" inside "latest"/"contest"/"protest").
        for phrase in ("what is the latest status?", "the contest results", "i protest this"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.harness.classify(phrase), TaskType.TRIVIAL)

    def test_inflected_keywords_still_match(self) -> None:
        # The leading-boundary match still catches plural/gerund forms.
        self.assertEqual(self.harness.classify("running the tests"), TaskType.IMPLEMENTATION)
        self.assertEqual(self.harness.classify("deploying to prod"), TaskType.RELEASE)
        self.assertEqual(self.harness.classify("planning the sprint"), TaskType.PLANNING)

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
