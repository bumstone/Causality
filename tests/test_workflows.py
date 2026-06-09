from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ouroboros_hitl.workflows import (
    OUROBOROS_WORKFLOWS,
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
        tdd = next(item for item in manifest["workflows"] if item["name"] == "test-driven-development")
        self.assertEqual(
            tdd["notes"],
            ["Do not skip the failing check when a regression can be expressed"],
        )

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
