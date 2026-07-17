from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.mcp_server import CausalityMCPServer


class MCPServerTests(unittest.TestCase):
    def test_server_blocks_pretracked_private_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            ledger = root / ".causality" / "ledger.jsonl"
            ledger.parent.mkdir()
            ledger.write_text("tracked legacy state\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "-f", ".causality/ledger.jsonl"], cwd=root, check=True
            )

            with self.assertRaisesRegex(ValueError, "already tracked by Git"):
                CausalityMCPServer(root)

            self.assertEqual(ledger.read_text(encoding="utf-8"), "tracked legacy state\n")

    def test_server_rejects_symlinked_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside:
            root = Path(temp_dir)
            try:
                (root / ".causality").symlink_to(Path(outside), target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                CausalityMCPServer(root)

            self.assertFalse((Path(outside) / "ledger.jsonl").exists())

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
            self.assertIn("verify", init["inputSchema"]["properties"])
            self.assertNotIn("force", init["inputSchema"]["properties"])
            self.assertNotIn("adopt", init["inputSchema"]["properties"])
            self.assertFalse(init["inputSchema"]["additionalProperties"])

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

    def test_init_tool_forwards_safe_activation_options(self) -> None:
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

    def test_init_tool_rejects_cli_only_mutation_options(self) -> None:
        for arguments in ({"adopt": True}, {"force": "false"}):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                agents = root / "AGENTS.md"
                agents.write_text("host rules", encoding="utf-8")
                server = CausalityMCPServer(root)

                response = server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "causality_init", "arguments": arguments},
                    }
                )

                self.assertEqual(response["error"]["code"], -32000)
                self.assertIn("CLI-only", response["error"]["message"])
                self.assertEqual(agents.read_text(encoding="utf-8"), "host rules")

    def test_init_tool_rejects_malformed_safe_options(self) -> None:
        for arguments in ({"verify": "false"}, {"client": 7}, {"client": "unknown"}):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temp_dir:
                server = CausalityMCPServer(temp_dir)
                response = server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {"name": "causality_init", "arguments": arguments},
                    }
                )

                self.assertEqual(response["error"]["code"], -32000)


if __name__ == "__main__":
    unittest.main()
