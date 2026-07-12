from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.workflows import (
    CONTROL_LAYERS,
    CAUSALITY_WORKFLOWS,
    build_session_bootstrap,
    build_subagent_packet,
    workflow_manifest,
)


class WorkflowTests(unittest.TestCase):
    def test_manifest_contains_expected_workflows(self) -> None:
        manifest = workflow_manifest()
        names = {item["name"] for item in manifest["workflows"]}

        self.assertIn("writing-plans", names)
        self.assertIn("verification-before-completion", names)
        self.assertIn("session-bootstrap", names)
        self.assertIn("a11y-observe", names)
        tdd = next(item for item in manifest["workflows"] if item["name"] == "test-driven-development")
        self.assertEqual(
            tdd["notes"],
            ["Do not skip the failing check when a regression can be expressed"],
        )

        browser = next(item for item in manifest["workflows"] if item["name"] == "a11y-observe")
        self.assertEqual(browser["gate"], "browser_action_gate")
        self.assertIn("current_state", browser["required_inputs"])

    def test_every_workflow_has_a_valid_control_layer(self) -> None:
        # ADR 0002: each workflow maps to one of the three control layers.
        for name, template in CAUSALITY_WORKFLOWS.items():
            self.assertIn(template.layer, CONTROL_LAYERS, f"{name} has invalid layer {template.layer!r}")

        by_layer = {layer: 0 for layer in CONTROL_LAYERS}
        for template in CAUSALITY_WORKFLOWS.values():
            by_layer[template.layer] += 1
        # every layer is represented
        for layer, count in by_layer.items():
            self.assertGreater(count, 0, f"no workflow tagged {layer}")

        manifest = workflow_manifest()
        self.assertTrue(all("layer" in item for item in manifest["workflows"]))

    def test_subagent_packet_is_seed_bound(self) -> None:
        packet = build_subagent_packet(
            seed_id="seed-1",
            task_id="task-1",
            allowed_tools=["Read"],
            context={"path": "src"},
        )

        self.assertEqual(packet["seed_id"], "seed-1")
        self.assertEqual(packet["expected_output"]["evidence_format"], "ledger_event_refs")

    def test_bootstrap_filters_unverified_memory(self) -> None:
        packet = build_session_bootstrap(
            active_seed={"id": "seed-1"},
            ledger_tail=[],
            memory_facts=[
                {"source": "tool-verified", "fact": "ok"},
                {"source": "agent-claim", "fact": "ignore"},
            ],
        )

        self.assertEqual(len(packet["memory_facts"]), 1)


if __name__ == "__main__":
    unittest.main()
