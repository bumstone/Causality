from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ouroboros_hitl.mcp_server import OuroborosMCPServer


class MCPServerTests(unittest.TestCase):
    def test_tools_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = OuroborosMCPServer(temp_dir)
            response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

            names = {tool["name"] for tool in response["result"]["tools"]}
            self.assertIn("ouroboros_context", names)
            self.assertIn("ouroboros_append_evidence", names)

    def test_append_evidence_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = OuroborosMCPServer(temp_dir)
            response = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "ouroboros_append_evidence",
                        "arguments": {"kind": "test_output", "payload": {"exit_code": 0}},
                    },
                }
            )

            self.assertIn("event_id", response["result"]["content"][0]["text"])
            self.assertTrue((Path(temp_dir) / ".ouroboros" / "ledger.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
