from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from causality.mcp_server import CausalityMCPServer
import test_mcp_server as mcp_support


class MCPSkillOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wire = mcp_support.MCPServerTests()

    def _verified_task(
        self,
        server: CausalityMCPServer,
        suffix: str,
        *,
        reflect: bool = False,
    ) -> tuple[str, str, dict[str, Any] | None]:
        task_id, _ = self.wire._begin(server, key=f"begin-{suffix}")
        _, verified = self.wire._call(
            server,
            "causality_task_verify",
            {
                "task_id": task_id,
                "idempotency_key": f"verify-{suffix}",
                "requirement_id": "wire-pass",
                "mode": "execute",
            },
        )
        evidence_hash = verified["event_hash"]
        for index, verifier in enumerate(("correctness", "evidence"), 1):
            self.wire._call(
                server,
                "causality_task_verdict",
                {
                    "task_id": task_id,
                    "idempotency_key": f"verdict-{suffix}-{index}",
                    "verifier": f"{verifier}-{suffix}",
                    "status": "pass",
                    "rationale": "independent check of exact verification evidence",
                    "severity": "normal",
                    "evidence_refs": [evidence_hash],
                },
            )
        _, completed = self.wire._call(
            server,
            "causality_task_complete",
            {"task_id": task_id, "idempotency_key": f"complete-{suffix}"},
        )
        self.assertEqual(completed["task"]["state"], "verified")
        reflected = None
        if reflect:
            _, reflected = self.wire._call(
                server,
                "causality_task_reflect",
                {"task_id": task_id, "idempotency_key": f"reflect-{suffix}"},
            )
        return task_id, evidence_hash, reflected

    def _call(
        self,
        server: CausalityMCPServer,
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.wire._call(server, name, arguments)

    def test_closed_schemas_and_proof_never_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            server = self.wire._server(td, approval_token="secret")
            tools = {item["name"]: item for item in server._tools()}
            for name in (
                "causality_skill_outcome",
                "causality_skill_promote",
                "causality_skill_recall",
            ):
                self.assertFalse(tools[name]["inputSchema"]["additionalProperties"])
            result = server._call_tool(
                "causality_skill_promote",
                {
                    "skill_id": "missing",
                    "idempotency_key": "k1",
                    "approved_by": "operator",
                    "evidence_refs": [],
                    "proof": "secret",
                },
            )
            payload = json.loads(result["content"][0]["text"])
            self.assertFalse(payload["ok"])
            self.assertNotIn("secret", json.dumps(payload))

    def test_verified_reflection_outcomes_promotion_and_recall_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            server = self.wire._server(root, approval_token="trusted")
            task1, ref1, reflected = self._verified_task(server, "one", reflect=True)
            assert reflected is not None
            candidate = reflected["data"]["skill"]
            skill_id = candidate["skill_id"]
            self.assertEqual(candidate["source_task_id"], task1)

            result, weak = self._call(
                server,
                "causality_skill_outcome",
                {
                    "task_id": task1,
                    "idempotency_key": "outcome-weak",
                    "skill_id": skill_id,
                    "success": True,
                    "evidence_refs": [],
                },
            )
            self.assertTrue(result["isError"])
            self.assertEqual(weak["error"]["code"], "evidence_scope_mismatch")

            outcome_refs: list[str] = []
            for index, (task_id, evidence_hash) in enumerate(
                [(task1, ref1), self._verified_task(server, "two")[:2], self._verified_task(server, "three")[:2]],
                1,
            ):
                outcome_refs.append(evidence_hash)
                result, outcome = self._call(
                    self.wire._server(root, approval_token="trusted"),
                    "causality_skill_outcome",
                    {
                        "task_id": task_id,
                        "idempotency_key": f"outcome-{index}",
                        "skill_id": skill_id,
                        "success": True,
                        "evidence_refs": [evidence_hash],
                    },
                )
                self.assertFalse(result.get("isError", False))
                self.assertFalse(outcome["idempotency"]["replayed"])

            restarted = self.wire._server(root, approval_token="trusted")
            _, replayed_reflection = self._call(
                restarted,
                "causality_task_reflect",
                {"task_id": task1, "idempotency_key": "reflect-one"},
            )
            self.assertEqual(replayed_reflection["data"]["skill"]["attempts"], 3)

            _, replayed_outcome = self._call(
                restarted,
                "causality_skill_outcome",
                {
                    "task_id": task1,
                    "idempotency_key": "outcome-one-retry",
                    "skill_id": skill_id,
                    "success": True,
                    "evidence_refs": [ref1],
                },
            )
            self.assertTrue(replayed_outcome["idempotency"]["replayed"])

            result, weak_promotion = self._call(
                restarted,
                "causality_skill_promote",
                {
                    "skill_id": skill_id,
                    "idempotency_key": "promote-weak",
                    "approved_by": "operator",
                    "evidence_refs": [ref1],
                    "proof": "trusted",
                },
            )
            self.assertTrue(result["isError"])
            self.assertEqual(
                weak_promotion["error"]["code"], "evidence_scope_mismatch"
            )

            promote_args = {
                "skill_id": skill_id,
                "idempotency_key": "promote-one",
                "approved_by": "operator",
                "evidence_refs": outcome_refs,
                "proof": "trusted",
            }
            _, promoted = self._call(restarted, "causality_skill_promote", promote_args)
            self.assertEqual(promoted["skill"]["promoted_by"], "operator")
            self.assertNotIn("proof", json.dumps(promoted))

            retry_args = copy.deepcopy(promote_args)
            retry_args["idempotency_key"] = "promote-retry"
            retry_args["evidence_refs"] = list(reversed(outcome_refs))
            _, replayed = self._call(
                self.wire._server(root, approval_token="trusted"),
                "causality_skill_promote",
                retry_args,
            )
            self.assertTrue(replayed["idempotency"]["replayed"])
            self.assertEqual(replayed["event_hash"], promoted["event_hash"])

            conflict_args = copy.deepcopy(promote_args)
            conflict_args["idempotency_key"] = "promote-conflict"
            conflict_args["approved_by"] = "another-operator"
            result, conflict = self._call(
                restarted,
                "causality_skill_promote",
                conflict_args,
            )
            self.assertTrue(result["isError"])
            self.assertEqual(conflict["error"]["code"], "idempotency_conflict")

            _, recalled = self._call(
                self.wire._server(root),
                "causality_skill_recall",
                {"objective": "exercise the durable MCP lifecycle", "limit": 10},
            )
            self.assertIn(skill_id, {item["skill_id"] for item in recalled["skills"]})
            events = server.ledger.events(all_segments=True)
            self.assertEqual(
                len([e for e in events if e.payload.get("kind") == "skill_outcome"]),
                3,
            )
            self.assertEqual(
                len([e for e in events if e.payload.get("kind") == "skill_promotion"]),
                1,
            )


if __name__ == "__main__":
    unittest.main()
