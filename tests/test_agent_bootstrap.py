from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.agent_bootstrap import install_agent_files


class AgentBootstrapTests(unittest.TestCase):
    def test_install_agent_files_writes_project_automation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = install_agent_files(temp_dir)
            root = Path(temp_dir)

            self.assertTrue((root / "AGENTS.md").is_file())
            self.assertTrue((root / "CLAUDE.md").is_file())
            self.assertTrue((root / ".claude" / "commands" / "onboard.md").is_file())
            self.assertTrue((root / ".claude" / "commands" / "causality-plan.md").is_file())
            self.assertTrue((root / ".claude" / "commands" / "causality-verify.md").is_file())
            self.assertTrue((root / ".codex" / "causality-routing.md").is_file())
            self.assertTrue((root / ".causality" / "agent-rules.md").is_file())
            self.assertTrue((root / ".causality" / "ledger.jsonl").is_file())
            self.assertTrue((root / ".causality" / "mcp.json").is_file())
            self.assertGreaterEqual(len(result.written), 11)

            mcp = json.loads((root / ".causality" / "mcp.json").read_text(encoding="utf-8"))
            self.assertIn("causality", mcp["mcpServers"])

    def test_install_agent_files_writes_context_economy_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_agent_files(temp_dir)
            root = Path(temp_dir)

            self.assertTrue((root / "workflow" / "README.md").is_file())
            self.assertTrue((root / "workflow" / "writing-plans.md").is_file())
            self.assertIn(
                "Layer: stage_designer",
                (root / "workflow" / "writing-plans.md").read_text(encoding="utf-8"),
            )
            self.assertTrue((root / "checklists" / "verification-before-completion.md").is_file())
            self.assertTrue((root / "skills" / "README.md").is_file())
            onboard_skill = root / "skills" / "onboard-project.md"
            self.assertTrue(onboard_skill.is_file())
            self.assertIn("Close every spawned subagent", onboard_skill.read_text(encoding="utf-8"))
            for mem_type in (
                "decisions",
                "assumptions",
                "failures",
                "playbooks",
                "snippets",
                "retrospectives",
            ):
                self.assertTrue((root / "memory" / mem_type / "README.md").is_file())

            rules = (root / ".causality" / "agent-rules.md").read_text(encoding="utf-8")
            self.assertIn("Context Economy", rules)

    def test_install_agent_files_does_not_overwrite_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "AGENTS.md").write_text("custom", encoding="utf-8")
            command = root / ".claude" / "commands" / "onboard.md"
            command.parent.mkdir(parents=True)
            command.write_text("custom command", encoding="utf-8")
            skill = root / "skills" / "onboard-project.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("custom skill", encoding="utf-8")

            result = install_agent_files(temp_dir)

            self.assertEqual((root / "AGENTS.md").read_text(encoding="utf-8"), "custom")
            self.assertEqual(command.read_text(encoding="utf-8"), "custom command")
            self.assertEqual(skill.read_text(encoding="utf-8"), "custom skill")
            self.assertIn(root / "AGENTS.md", result.skipped)
            self.assertIn(command, result.skipped)
            self.assertIn(skill, result.skipped)


if __name__ == "__main__":
    unittest.main()
