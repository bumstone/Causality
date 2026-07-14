from __future__ import annotations

import codecs
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
import venv
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.agent_bootstrap import (
    LEGACY_CLAUDE_MD,
    ClientProbeResult,
    _probe_claude,
    _probe_codex,
    _trusted_client_executable,
    install_agent_files,
    mcp_config,
)


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


class AgentBootstrapTests(unittest.TestCase):
    def test_mcp_config_pins_current_interpreter(self) -> None:
        config = mcp_config(Path("project").resolve())

        self.assertEqual(config["mcpServers"]["causality"]["command"], sys.executable)
        self.assertEqual(config["mcpServers"]["causality"]["args"][0], "-I")

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

    def test_install_rejects_symlinked_generated_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside:
            root = Path(temp_dir)
            linked_skills = root / "skills"
            try:
                linked_skills.symlink_to(Path(outside), target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                install_agent_files(root)

            self.assertFalse((Path(outside) / "onboard-project.md").exists())
            self.assertFalse((root / ".causality").exists())

    def test_install_rejects_symlinked_causality_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside:
            root = Path(temp_dir)
            linked_causality = root / ".causality"
            try:
                linked_causality.symlink_to(Path(outside), target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                install_agent_files(root)

            self.assertFalse((Path(outside) / "ledger.jsonl").exists())

    def test_install_rejects_symlinked_native_config_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside:
            root = Path(temp_dir)
            linked_codex = root / ".codex"
            try:
                linked_codex.symlink_to(Path(outside), target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                install_agent_files(root, client="codex")

            self.assertFalse((Path(outside) / "config.toml").exists())

    def test_install_rejects_symlinked_runtime_files(self) -> None:
        for filename in (
            "mcp.json",
            "install-report.json",
            "install-report.json.lock",
            "ledger.jsonl",
            "ledger.jsonl.lock",
        ):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                causality_dir = root / ".causality"
                causality_dir.mkdir()
                outside = root / f"outside-{filename}"
                outside.write_text("outside", encoding="utf-8")
                try:
                    (causality_dir / filename).symlink_to(outside)
                except OSError as exc:
                    self.skipTest(f"file symlinks unavailable: {exc}")

                with self.assertRaisesRegex(ValueError, "contains a symlink"):
                    install_agent_files(root, client="generic")

                self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_force_replaces_hardlinked_generated_file_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside:
            root = Path(temp_dir)
            portable = root / ".causality" / "mcp.json"
            portable.parent.mkdir(parents=True)
            source = Path(outside) / "mcp.json"
            source.write_text("outside", encoding="utf-8")
            try:
                os.link(source, portable)
            except OSError as exc:
                self.skipTest(f"hard links unavailable: {exc}")

            result = install_agent_files(root, client="generic", force=True)

            self.assertNotEqual(result.activation, "broken")
            self.assertEqual(source.read_text(encoding="utf-8"), "outside")
            self.assertFalse(os.path.samefile(source, portable))

    def test_install_self_ignores_private_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            causality_dir = root / ".causality"
            causality_dir.mkdir()
            (causality_dir / ".gitignore").write_text(
                "# BEGIN CAUSALITY PRIVATE\n*\n!.gitignore\n"
                "# END CAUSALITY PRIVATE\n!ledger.jsonl\n",
                encoding="utf-8",
            )

            install_agent_files(root, client="generic")

            ignored = subprocess.run(
                ["git", "check-ignore", ".causality/ledger.jsonl"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            status = subprocess.run(
                ["git", "status", "--short", "--untracked-files=all"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(ignored.returncode, 0)
            self.assertNotIn("ledger.jsonl", status)
            self.assertNotIn("install-report.json", status)
            self.assertIn(".causality/.gitignore", status)

    def test_install_blocks_pretracked_private_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            ledger = root / ".causality" / "ledger.jsonl"
            ledger.parent.mkdir()
            ledger.write_text("tracked legacy state\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "-f", ".causality/ledger.jsonl"], cwd=root, check=True
            )

            result = install_agent_files(root, client="generic")

            self.assertEqual(result.activation, "broken")
            self.assertIsNone(result.report_path)
            self.assertEqual(ledger.read_text(encoding="utf-8"), "tracked legacy state\n")
            self.assertFalse((root / ".causality" / ".gitignore").exists())
            self.assertIn("git rm -r --cached", " ".join(result.remediation))

    def test_install_blocks_pretracked_private_path_outside_windows_acp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            private = root / ".causality" / "🔒.txt"
            private.parent.mkdir()
            private.write_text("private", encoding="utf-8")
            subprocess.run(
                ["git", "add", "-f", ".causality/🔒.txt"], cwd=root, check=True
            )

            result = install_agent_files(root, client="generic")

            self.assertEqual(result.activation, "broken")
            self.assertIn("🔒.txt", " ".join(result.remediation))

    @unittest.skipUnless(os.name == "nt", "Windows case-folding regression")
    def test_install_blocks_case_variant_pretracked_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            ledger = root / ".Causality" / "ledger.jsonl"
            ledger.parent.mkdir()
            ledger.write_text("tracked legacy state\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "-f", ".Causality/ledger.jsonl"], cwd=root, check=True
            )

            result = install_agent_files(root, client="generic")

            self.assertEqual(result.activation, "broken")
            self.assertEqual(ledger.read_text(encoding="utf-8"), "tracked legacy state\n")

    def test_install_fails_closed_when_project_local_git_hides_trusted_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            ledger = root / ".causality" / "ledger.jsonl"
            ledger.parent.mkdir()
            ledger.write_text("tracked legacy state\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "-f", ".causality/ledger.jsonl"], cwd=root, check=True
            )
            local_git = root / ("git.cmd" if os.name == "nt" else "git")
            local_git.write_text("local git must not run", encoding="utf-8")
            if os.name != "nt":
                local_git.chmod(0o755)

            with mock.patch.dict(os.environ, {"PATH": str(root)}):
                result = install_agent_files(root, client="generic")

            self.assertEqual(result.activation, "broken")
            self.assertEqual(ledger.read_text(encoding="utf-8"), "tracked legacy state\n")
            self.assertIn("trusted Git executable", " ".join(result.remediation))

    def test_install_fails_closed_when_git_tracking_query_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            with mock.patch(
                "causality.agent_bootstrap.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="query failed"),
            ):
                result = install_agent_files(root, client="generic")

            self.assertEqual(result.activation, "broken")
            self.assertIn("query failed", " ".join(result.remediation))

    def test_adopt_rejects_symlinked_host_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside-agents.md"
            outside.write_text("host rules", encoding="utf-8")
            try:
                (root / "AGENTS.md").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                install_agent_files(root, client="codex", adopt=True)

            self.assertEqual(outside.read_text(encoding="utf-8"), "host rules")

    def test_force_preserves_host_owned_entrypoints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agents = root / "AGENTS.md"
            claude = root / "CLAUDE.md"
            agents.write_text("host agents", encoding="utf-8")
            claude.write_text("host claude", encoding="utf-8")
            generated = root / ".causality" / "agent-rules.md"
            generated.parent.mkdir(parents=True)
            generated.write_text("stale generated rules", encoding="utf-8")

            result = install_agent_files(temp_dir, force=True)

            self.assertEqual(agents.read_text(encoding="utf-8"), "host agents")
            self.assertEqual(claude.read_text(encoding="utf-8"), "host claude")
            self.assertNotEqual(
                generated.read_text(encoding="utf-8"), "stale generated rules"
            )
            self.assertIn(agents, result.skipped)
            self.assertIn(claude, result.skipped)
            self.assertIn(generated, result.written)

    def test_force_migrates_legacy_portable_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            portable = root / ".causality" / "mcp.json"
            portable.parent.mkdir(parents=True)
            portable.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "causality": {
                                "command": "python",
                                "args": ["-m", "causality.mcp_server", "--project", str(root)],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            result = install_agent_files(root, client="generic", force=True)

            updated = json.loads(portable.read_text(encoding="utf-8"))
            self.assertNotEqual(result.activation, "broken")
            self.assertEqual(updated["mcpServers"]["causality"]["command"], sys.executable)

    def test_existing_host_requires_adopt_and_preserves_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agents = root / "AGENTS.md"
            original = codecs.BOM_UTF8 + b"# Host rules\r\n\r\nKeep this.\r\n"
            agents.write_bytes(original)

            result = install_agent_files(root, client="codex")

            self.assertEqual(agents.read_bytes(), original)
            self.assertEqual(result.activation, "pending")
            self.assertIn(".causality/agent-rules.md", "\n".join(result.remediation))

    def test_adopt_marker_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agents = root / "AGENTS.md"
            agents.write_text("# Host rules\n", encoding="utf-8")

            install_agent_files(root, client="codex", adopt=True)
            first = agents.read_bytes()
            install_agent_files(root, client="codex", adopt=True)
            second = agents.read_bytes()

            self.assertEqual(first, second)
            text = second.decode("utf-8")
            self.assertEqual(text.count("BEGIN CAUSALITY ROUTING"), 1)
            self.assertEqual(text.count("END CAUSALITY ROUTING"), 1)
            self.assertIn(".causality/agent-rules.md", text)

    def test_native_client_configs_merge_and_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_config = root / ".codex" / "config.toml"
            codex_config.parent.mkdir(parents=True)
            codex_config.write_bytes(
                b'[mcp_servers.other]\r\ncommand = "other"\r\n'
            )

            install_agent_files(root, client="codex")
            codex_first = codex_config.read_bytes()
            self.assertNotIn(b"\r\r\n", codex_first)
            self.assertIn(b"\r\n", codex_first)
            install_agent_files(root, client="codex")
            self.assertEqual(codex_config.read_bytes(), codex_first)
            codex = tomllib.loads(codex_first.decode("utf-8"))
            self.assertEqual(codex["mcp_servers"]["other"]["command"], "other")
            self.assertEqual(codex["mcp_servers"]["causality"]["command"], sys.executable)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            claude_config = root / ".mcp.json"
            claude_config.write_text(
                json.dumps({"mcpServers": {"other": {"command": "other"}}}),
                encoding="utf-8",
            )

            install_agent_files(root, client="claude")
            claude_first = claude_config.read_bytes()
            install_agent_files(root, client="claude")
            self.assertEqual(claude_config.read_bytes(), claude_first)
            claude = json.loads(claude_first.decode("utf-8"))
            self.assertEqual(claude["mcpServers"]["other"]["command"], "other")
            self.assertEqual(claude["mcpServers"]["causality"]["command"], sys.executable)

    def test_verify_generic_runs_handshake_and_writes_active_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(os.environ, {"PYTHONPATH": str(SRC_ROOT)}):
                result = install_agent_files(temp_dir, client="generic", verify=True)

            self.assertEqual(result.activation, "active")
            self.assertEqual(result.handshake.status, "pass")
            report_path = Path(temp_dir) / ".causality" / "install-report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["activation"], "active")
            self.assertEqual(report["interpreter"], sys.executable)
            self.assertEqual(report["handshake"]["status"], "pass")
            self.assertTrue(report["timestamp"].endswith("+00:00"))

    def test_broken_interpreter_keeps_assets_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing = root / "missing-python"

            result = install_agent_files(
                root,
                client="generic",
                verify=True,
                interpreter=missing,
            )

            self.assertEqual(result.activation, "broken")
            self.assertEqual(result.handshake.status, "fail")
            self.assertTrue((root / ".causality" / "agent-rules.md").is_file())
            report_path = root / ".causality" / "install-report.json"
            self.assertTrue(report_path.is_file())
            self.assertIn("interpreter", " ".join(result.remediation).lower())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            event = json.loads(
                (root / ".causality" / "ledger.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            self.assertEqual(report["activation"], "broken")
            self.assertEqual(event["payload"]["activation"], "broken")
            self.assertIn(str(report_path), [item["path"] for item in event["artifacts"]])

    def test_auto_with_multiple_client_signals_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "AGENTS.md").write_text("codex", encoding="utf-8")
            (root / "CLAUDE.md").write_text("claude", encoding="utf-8")

            result = install_agent_files(root, client="auto")

            self.assertIsNone(result.resolved_client)
            self.assertEqual(result.activation, "pending")
            self.assertIn("--client", " ".join(result.remediation))

    def test_auto_reuses_previous_resolved_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first = install_agent_files(temp_dir, client="codex")
            second = install_agent_files(temp_dir, client="auto")

            self.assertEqual(first.resolved_client, "codex")
            self.assertEqual(second.resolved_client, "codex")

    def test_client_probe_controls_active_vs_pending(self) -> None:
        for probe_status, expected in (("pass", "active"), ("pending", "pending")):
            with self.subTest(probe_status=probe_status), tempfile.TemporaryDirectory() as temp_dir:
                with (
                    mock.patch.dict(os.environ, {"PYTHONPATH": str(SRC_ROOT)}),
                    mock.patch(
                        "causality.agent_bootstrap._probe_client",
                        return_value=ClientProbeResult(probe_status, "probe result"),
                    ),
                ):
                    result = install_agent_files(temp_dir, client="codex", verify=True)

                self.assertEqual(result.activation, expected)

    def test_malformed_native_config_and_marker_are_broken_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / ".codex" / "config.toml"
            config.parent.mkdir(parents=True)
            config.write_text("not = [valid", encoding="utf-8")
            original = config.read_bytes()
            agents = root / "AGENTS.md"
            agents.write_text("# Host rules\n", encoding="utf-8")
            original_agents = agents.read_bytes()

            result = install_agent_files(root, client="codex", adopt=True)

            self.assertEqual(result.activation, "broken")
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(agents.read_bytes(), original_agents)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agents = root / "AGENTS.md"
            agents.write_text(
                "# Host\n<!-- BEGIN CAUSALITY ROUTING -->\n", encoding="utf-8"
            )
            original = agents.read_bytes()

            result = install_agent_files(root, client="codex", adopt=True)

            self.assertEqual(result.activation, "broken")
            self.assertEqual(agents.read_bytes(), original)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / ".codex" / "config.toml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "# END CAUSALITY MCP\n# BEGIN CAUSALITY MCP\n", encoding="utf-8"
            )
            original = config.read_bytes()

            result = install_agent_files(root, client="codex")

            self.assertEqual(result.activation, "broken")
            self.assertEqual(config.read_bytes(), original)
            self.assertTrue((root / ".causality" / "install-report.json").is_file())

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / ".codex" / "config.toml"
            config.parent.mkdir(parents=True)
            config.write_text('mcp_servers = "host-value"\n', encoding="utf-8")

            result = install_agent_files(root, client="codex")

            self.assertEqual(result.activation, "broken")
            self.assertTrue((root / ".causality" / "install-report.json").is_file())

    def test_routing_requires_ordered_positive_instruction(self) -> None:
        samples = (
            "<!-- END CAUSALITY ROUTING -->\n<!-- BEGIN CAUSALITY ROUTING -->\n"
            "Follow `.causality/agent-rules.md`\n",
            "<!-- BEGIN CAUSALITY ROUTING -->\n# Empty\n<!-- END CAUSALITY ROUTING -->\n"
            "Follow `.causality/agent-rules.md`\n",
            "Do not follow `.causality/agent-rules.md`.\n",
            "Do not Follow `.causality/agent-rules.md`.\n",
        )
        for content in samples:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                (root / "AGENTS.md").write_text(content, encoding="utf-8")

                result = install_agent_files(root, client="codex")

                self.assertNotEqual(result.activation, "active")
                report = json.loads(
                    (root / ".causality" / "install-report.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(report["routing"]["codex"]["status"], "broken")

    def test_force_refreshes_installer_owned_claude_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_python = root / "venv-a" / "python"
            second_python = root / "venv-b" / "python"
            install_agent_files(root, client="claude", interpreter=first_python)

            result = install_agent_files(
                root,
                client="claude",
                interpreter=second_python,
                force=True,
            )

            config = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
            self.assertNotEqual(result.activation, "broken")
            self.assertEqual(
                config["mcpServers"]["causality"]["command"], str(second_python)
            )

    def test_force_accepts_legacy_generated_claude_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            claude = root / "CLAUDE.md"
            claude.write_text(LEGACY_CLAUDE_MD, encoding="utf-8")

            result = install_agent_files(root, client="claude", force=True)

            self.assertNotEqual(result.activation, "broken")
            self.assertEqual(claude.read_text(encoding="utf-8"), LEGACY_CLAUDE_MD)

    def test_client_probes_reject_unstructured_or_mismatched_output(self) -> None:
        root = Path.cwd()
        server = mcp_config(root)["mcpServers"]["causality"]
        with (
            mock.patch("causality.agent_bootstrap.shutil.which", return_value="codex"),
            mock.patch(
                "causality.agent_bootstrap.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout='{"name": "causality"}', stderr=""),
            ),
        ):
            codex = _probe_codex(root, server, 1)
        with (
            mock.patch("causality.agent_bootstrap.shutil.which", return_value="claude"),
            mock.patch(
                "causality.agent_bootstrap.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ),
        ):
            claude = _probe_claude(root, server, 1)

        self.assertNotEqual(codex.status, "pass")
        self.assertNotEqual(claude.status, "pass")

    def test_client_probe_does_not_execute_project_local_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "probe-executed"
            if os.name == "nt":
                executable = root / "codex.cmd"
                executable.write_text(f"@echo malicious>\"{marker}\"\r\n", encoding="utf-8")
            else:
                executable = root / "codex"
                executable.write_text(f"#!/bin/sh\necho malicious > '{marker}'\n", encoding="utf-8")
                executable.chmod(0o755)

            with mock.patch.dict(os.environ, {"PATH": str(root)}):
                result = _probe_codex(root, mcp_config(root)["mcpServers"]["causality"], 1)

            self.assertEqual(result.status, "pending")
            self.assertFalse(marker.exists())

    def test_client_probe_rejects_project_symlink_to_trusted_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "probe-executed"
            executable = root / ("codex.cmd" if os.name == "nt" else "codex")
            try:
                executable.symlink_to(Path(sys.executable))
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            mcp = root / "mcp"
            mcp.write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"PATH": str(root)}):
                result = _probe_codex(root, mcp_config(root)["mcpServers"]["causality"], 1)

            self.assertEqual(result.status, "pending")
            self.assertFalse(marker.exists())

    def test_client_probe_allows_external_executable_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside:
            root = Path(temp_dir)
            executable = Path(outside) / ("codex.exe" if os.name == "nt" else "codex")
            try:
                executable.symlink_to(Path(sys.executable))
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            with mock.patch(
                "causality.agent_bootstrap.shutil.which", return_value=str(executable)
            ):
                trusted = _trusted_client_executable("codex", root)

            self.assertEqual(trusted, str(Path(sys.executable).resolve()))

    def test_venv_interpreter_runs_external_project_handshake(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            environment = base / "venv"
            project = base / "host project"
            project.mkdir()
            marker = project / "shadow-executed"
            shadow = project / "causality"
            shadow.mkdir()
            (shadow / "__init__.py").write_text("", encoding="utf-8")
            (shadow / "mcp_server.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            venv.EnvBuilder(with_pip=False).create(environment)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            purelib = subprocess.run(
                [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            Path(purelib, "causality-local.pth").write_text(str(SRC_ROOT), encoding="utf-8")
            clean_env = os.environ.copy()
            clean_env["PYTHONPATH"] = str(project)
            installer_launcher = (
                "import runpy,sys;"
                f"sys.path.insert(0,{str(SRC_ROOT)!r});"
                "runpy.run_module('causality.cli',run_name='__main__')"
            )

            completed = subprocess.run(
                [
                    str(python),
                    "-I",
                    "-c",
                    installer_launcher,
                    "install-agent",
                    "--project",
                    str(project),
                    "--client",
                    "generic",
                    "--verify",
                ],
                capture_output=True,
                text=True,
                env=clean_env,
                check=False,
                timeout=30,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            self.assertEqual(output["activation"], "active")
            self.assertFalse(marker.exists())
            config = json.loads((project / ".causality" / "mcp.json").read_text(encoding="utf-8"))
            self.assertEqual(config["mcpServers"]["causality"]["command"], str(python))


if __name__ == "__main__":
    unittest.main()
