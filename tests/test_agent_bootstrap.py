from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ouroboros_hitl.agent_bootstrap import install_agent_files


class AgentBootstrapTests(unittest.TestCase):
    def test_install_agent_files_writes_project_automation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = install_agent_files(temp_dir)
            root = Path(temp_dir)

            self.assertTrue((root / "AGENTS.md").is_file())
            self.assertTrue((root / "CLAUDE.md").is_file())
            self.assertTrue((root / ".claude" / "commands" / "ouroboros-plan.md").is_file())
            self.assertTrue((root / ".claude" / "commands" / "ouroboros-verify.md").is_file())
            self.assertTrue((root / ".codex" / "ouroboros-routing.md").is_file())
            self.assertTrue((root / ".ouroboros" / "agent-rules.md").is_file())
            self.assertTrue((root / ".ouroboros" / "ledger.jsonl").is_file())
            self.assertTrue((root / ".ouroboros" / "mcp.json").is_file())
            self.assertGreaterEqual(len(result.written), 11)

            mcp = json.loads((root / ".ouroboros" / "mcp.json").read_text(encoding="utf-8"))
            self.assertIn("ouroboros-hitl", mcp["mcpServers"])

    def test_install_agent_files_does_not_overwrite_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "AGENTS.md").write_text("custom", encoding="utf-8")

            result = install_agent_files(temp_dir)

            self.assertEqual((root / "AGENTS.md").read_text(encoding="utf-8"), "custom")
            self.assertIn(root / "AGENTS.md", result.skipped)


if __name__ == "__main__":
    unittest.main()
