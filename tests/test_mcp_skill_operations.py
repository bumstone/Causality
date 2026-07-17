from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import GoalContract
from causality.ledger import EvidenceLedger
from causality.mcp_server import CausalityMCPServer
from causality.skills import SkillStore


class MCPSkillOperationTests(unittest.TestCase):
    def test_skill_tools_are_closed_and_promotion_hides_proof(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            server = CausalityMCPServer(root, approval_token="secret")
            names = {item["name"]: item for item in server._tools()}
            self.assertIn("causality_skill_outcome", names)
            self.assertFalse(names["causality_skill_promote"]["inputSchema"]["additionalProperties"])
            result = server._call_tool("causality_skill_promote", {
                "skill_id": "missing", "idempotency_key": "k1", "approved_by": "operator",
                "evidence_refs": [], "proof": "secret",
            })
            payload = json.loads(result["content"][0]["text"])
            self.assertFalse(payload["ok"])
            self.assertNotIn("secret", json.dumps(payload))

    def test_distilled_candidate_is_replayable_through_store(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger = EvidenceLedger(root / ".causality" / "ledger.jsonl")
            contract = GoalContract(title="skill", summary="skill")
            ledger.append("goal_contract", {"title": "skill"}, contract_id=contract.goal_id)
            store = SkillStore(root)
            first = store.distill_once(ledger, contract, skill_id="stable", source_task_id="task")
            second = store.distill_once(ledger, contract, skill_id="stable", source_task_id="task")
            self.assertEqual(first.to_dict(), second.to_dict())
