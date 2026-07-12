from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.mcp_server import CausalityMCPServer


class MCPServerTests(unittest.TestCase):
    def test_tools_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = CausalityMCPServer(temp_dir)
            response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

            names = {tool["name"] for tool in response["result"]["tools"]}
            self.assertIn("causality_context", names)
            self.assertIn("causality_append_evidence", names)
            init = next(
                tool for tool in response["result"]["tools"] if tool["name"] == "causality_init"
            )
            self.assertIn("client", init["inputSchema"]["properties"])
            self.assertIn("adopt", init["inputSchema"]["properties"])
            self.assertIn("verify", init["inputSchema"]["properties"])

    def test_append_evidence_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = CausalityMCPServer(temp_dir)
            response = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "causality_append_evidence",
                        "arguments": {"kind": "test_output", "payload": {"exit_code": 0}},
                    },
                }
            )

            self.assertIn("event_id", response["result"]["content"][0]["text"])
            self.assertTrue((Path(temp_dir) / ".causality" / "ledger.jsonl").is_file())

    def test_context_tool_omits_raw_ledger_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = CausalityMCPServer(temp_dir)
            sentinel = "context-secret-sentinel"
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "causality_append_evidence",
                        "arguments": {
                            "kind": "test_output",
                            "payload": {"token": sentinel},
                            "contract_id": f"contract-{sentinel}",
                        },
                    },
                }
            )

            response = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "causality_context", "arguments": {"limit": 5}},
                }
            )
            text = response["result"]["content"][0]["text"]
            context = json.loads(text)

            self.assertNotIn(sentinel, text)
            self.assertNotIn("payload", context["ledger_tail"][0])
            self.assertNotIn("contract_id", context["ledger_tail"][0])

    def test_init_tool_forwards_activation_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = CausalityMCPServer(temp_dir)
            response = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "causality_init",
                        "arguments": {
                            "client": "generic",
                            "adopt": True,
                            "verify": False,
                        },
                    },
                }
            )

            result = json.loads(response["result"]["content"][0]["text"])
            self.assertEqual(result["resolved_client"], "generic")
            self.assertEqual(result["activation"], "pending")
            self.assertTrue(
                (Path(temp_dir) / ".causality" / "install-report.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()
