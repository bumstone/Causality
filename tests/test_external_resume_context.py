from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_mcp_external as _external_support


REPO_ROOT = _external_support.REPO_ROOT
SRC_ROOT = _external_support.SRC_ROOT
_tree_snapshot = _external_support._tree_snapshot
PHASE_ID = "root-cause-protocol/reproduce"


def _semantic_state(project: Path) -> dict[str, bytes]:
    paths = [
        *sorted(
            path
            for path in (project / ".causality").glob("ledger.jsonl*")
            if path.name == "ledger.jsonl"
            or path.name.removeprefix("ledger.jsonl.").split(".", 1)[0].isdigit()
        ),
        *sorted(project.glob("memory/**/*.jsonl")),
        *sorted(project.glob("skills/**/*.jsonl")),
    ]
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in paths
        if path.is_file()
    }


class ExternalResumeContextTests(unittest.TestCase):
    _request = staticmethod(_external_support.ExternalMCPTests._request)
    _exchange = _external_support.ExternalMCPTests._exchange
    _finish_server = _external_support.ExternalMCPTests._finish_server
    _server = _external_support.ExternalMCPTests._server

    request_id: int

    def _call(
        self,
        process: subprocess.Popen[str],
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float = 45,
    ) -> dict[str, Any]:
        payload = self._exchange(
            process,
            self._request(self.request_id, name, arguments),
            timeout=timeout,
        )
        self.request_id += 1
        return payload

    @staticmethod
    def _begin(
        verification_argv: list[str],
        *,
        objective: str,
        key: str,
        workflow: str | None = None,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "objective": objective,
            "summary": "installed resume/context acceptance",
            "risk": "low",
            "permissions": {
                "allowed_tools": ["file.write", "shell"],
                "write_scope": ["out"],
                "network_scope": [],
                "auth_scope": [],
            },
            "verification_requirements": [
                {
                    "id": "external-resume",
                    "argv": verification_argv,
                    "expected_exit_codes": [0],
                    "timeout_seconds": 60,
                    "artifact_paths": {"out/result.txt": None},
                    "required": True,
                    "manual": False,
                }
            ],
            "stop_condition": {
                "max_iterations": 8,
                "max_failed_hypotheses": 3,
                "no_progress_iterations": 2,
            },
            "non_goals": ["write outside the installed external project"],
            "idempotency_key": key,
        }
        if workflow is not None:
            value["workflow"] = workflow
        return value

    def test_installed_resume_and_context_survive_three_processes_without_replay(
        self,
    ) -> None:
        repo_build = REPO_ROOT / "build"
        build_before = _tree_snapshot(repo_build)
        self.addCleanup(
            lambda: self.assertEqual(
                _tree_snapshot(repo_build),
                build_before,
                "external installation must not pollute the source build tree",
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            environment = base / "fresh venv"
            project = base / "external project"
            package_source = base / "package source"
            project.mkdir()
            package_source.mkdir()
            (project / "test_resume_target.py").write_text(
                "import unittest\n"
                "from pathlib import Path\n\n"
                "class ResumeTarget(unittest.TestCase):\n"
                "    def test_result(self):\n"
                "        self.assertEqual(\n"
                "            Path('out/result.txt').read_text(encoding='utf-8'),\n"
                "            'one terminal effect\\n',\n"
                "        )\n",
                encoding="utf-8",
            )
            (project / "memory").mkdir()
            (project / "memory" / "README.md").write_text(
                "# Curated memory\n", encoding="utf-8"
            )
            (project / "skills").mkdir()
            (project / "skills" / "curated.md").write_text(
                "# Curated skill\n", encoding="utf-8"
            )
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy2(REPO_ROOT / name, package_source / name)
            shutil.copytree(
                SRC_ROOT,
                package_source / "src",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

            venv.EnvBuilder(with_pip=True).create(environment)
            python = environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            clean_env = {
                name: value
                for name, value in os.environ.items()
                if not name.startswith("CAUSALITY_") and name != "PYTHONPATH"
            }
            clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
            installed = subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--no-deps",
                    str(package_source),
                ],
                cwd=project,
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            imported = subprocess.run(
                [
                    str(python),
                    "-c",
                    "from pathlib import Path; import causality; "
                    "print(Path(causality.__file__).resolve())",
                ],
                cwd=project,
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            import_path = Path(imported.stdout.strip())
            self.assertTrue(import_path.is_relative_to(environment.resolve()))
            self.assertFalse(import_path.is_relative_to(package_source.resolve()))
            self.assertFalse(import_path.is_relative_to(REPO_ROOT.resolve()))

            memory_script = "\n".join(
                (
                    "from causality.memory import TypedMemory",
                    "memory = TypedMemory('.')",
                    "active = memory.record_failure(",
                    "    'active installed failure', scope='external', ttl_days=30)",
                    "memory.record_once(",
                    "    'failures', 'expired installed failure',",
                    "    entry_id='expired-installed',",
                    "    created_at='2000-01-01T00:00:00+00:00',",
                    "    scope='external', ttl_days=1)",
                    "print(active.entry_id)",
                )
            )
            seeded = subprocess.run(
                [str(python), "-c", memory_script],
                cwd=project,
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(seeded.returncode, 0, seeded.stderr)
            active_id = seeded.stdout.strip()

            verification_argv = [
                str(python),
                "-m",
                "unittest",
                "discover",
                "-s",
                ".",
                "-p",
                "test_resume_target.py",
                "-v",
            ]
            server_env = dict(clean_env)
            server_env["CAUSALITY_VERIFICATION_COMMANDS_JSON"] = json.dumps(
                [verification_argv]
            )
            self.request_id = 1

            with self._server(python, project, server_env) as process_a:
                begun = self._call(
                    process_a,
                    "causality_task_begin",
                    self._begin(
                        verification_argv,
                        objective="debug an interrupted installed phase",
                        key="external-mid-begin",
                        workflow="root-cause-protocol",
                    ),
                )
                mid_task_id = begun["task"]["task_id"]
                started = self._call(
                    process_a,
                    "causality_task_phase",
                    {
                        "task_id": mid_task_id,
                        "idempotency_key": "external-mid-start",
                        "phase_id": PHASE_ID,
                        "action": "start",
                    },
                )
                self.assertEqual(started["task"]["current_phase_id"], PHASE_ID)
                self.assertEqual(
                    started["task"]["workflow_phases"][0]["status"], "running"
                )

            rotated = subprocess.run(
                [
                    str(python),
                    "-c",
                    "from causality.ledger import EvidenceLedger; "
                    "EvidenceLedger('.causality/ledger.jsonl').rotate()",
                ],
                cwd=project,
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(rotated.returncode, 0, rotated.stderr)

            before_mid_resume = _semantic_state(project)
            with self._server(python, project, server_env) as process_b:
                resumed_mid = self._call(
                    process_b,
                    "causality_task_resume",
                    {"task_id": mid_task_id},
                )
                self.assertEqual(resumed_mid["task"], started["task"])
                self.assertEqual(resumed_mid["task"]["current_phase_id"], PHASE_ID)
                self.assertEqual(
                    resumed_mid["data"]["unmet_verification"], ["external-resume"]
                )
                self.assertNotIn("idempotency", resumed_mid)
                self.assertNotIn("event_hash", resumed_mid)
                self.assertEqual(_semantic_state(project), before_mid_resume)

                terminal_begin = self._call(
                    process_b,
                    "causality_task_begin",
                    self._begin(
                        verification_argv,
                        objective="catalog durable task state",
                        key="external-terminal-begin",
                    ),
                )
                terminal_task_id = terminal_begin["task"]["task_id"]
                self.assertEqual(terminal_begin["task"]["workflow_phases"], [])
                self._call(
                    process_b,
                    "causality_task_action",
                    {
                        "task_id": terminal_task_id,
                        "idempotency_key": "external-terminal-action",
                        "action": {
                            "kind": "file_write",
                            "path": "out/result.txt",
                            "content": "one terminal effect\n",
                        },
                    },
                )
                verified = self._call(
                    process_b,
                    "causality_task_verify",
                    {
                        "task_id": terminal_task_id,
                        "idempotency_key": "external-terminal-verify",
                        "requirement_id": "external-resume",
                        "mode": "execute",
                    },
                    timeout=90,
                )
                self.assertEqual(verified["data"]["status"], "pass")
                for index, verifier in enumerate(
                    ("external-resume-correctness", "external-resume-evidence"),
                    start=1,
                ):
                    self._call(
                        process_b,
                        "causality_task_verdict",
                        {
                            "task_id": terminal_task_id,
                            "idempotency_key": f"external-terminal-verdict-{index}",
                            "verifier": verifier,
                            "status": "pass",
                            "rationale": f"{verifier} reviewed installed evidence",
                            "evidence_refs": [verified["event_hash"]],
                        },
                    )
                completed = self._call(
                    process_b,
                    "causality_task_complete",
                    {
                        "task_id": terminal_task_id,
                        "idempotency_key": "external-terminal-complete",
                    },
                )
                self.assertEqual(completed["task"]["state"], "verified")
                reflected = self._call(
                    process_b,
                    "causality_task_reflect",
                    {
                        "task_id": terminal_task_id,
                        "idempotency_key": "external-terminal-reflect",
                        "scope": "external-resume",
                        "ttl_days": 30,
                    },
                )
                looked_up = self._call(
                    process_b,
                    "causality_task_resume",
                    {"task_id": terminal_task_id},
                )
                self.assertEqual(looked_up["data"]["unmet_verification"], [])
                self.assertEqual(
                    looked_up["data"]["terminal_result"]["event_hash"],
                    completed["event_hash"],
                )
                self.assertEqual(
                    looked_up["data"]["reflection_result"]["event_hash"],
                    reflected["event_hash"],
                )
                self.assertEqual(
                    (project / "out" / "result.txt").read_text(encoding="utf-8"),
                    "one terminal effect\n",
                )

            target = project / "out" / "result.txt"
            os.utime(target, (1, 1))
            target_mtime = target.stat().st_mtime_ns
            before_read_process = _semantic_state(project)
            with self._server(python, project, server_env) as process_c:
                resumed_mid_again = self._call(
                    process_c,
                    "causality_task_resume",
                    {"task_id": mid_task_id},
                )
                terminal_once = self._call(
                    process_c,
                    "causality_task_resume",
                    {"task_id": terminal_task_id},
                )
                terminal_twice = self._call(
                    process_c,
                    "causality_task_resume",
                    {"task_id": terminal_task_id},
                )
                context = self._call(
                    process_c,
                    "causality_context",
                    {"limit": 10},
                )

            self.assertEqual(resumed_mid_again, resumed_mid)
            self.assertEqual(terminal_once, terminal_twice)
            self.assertEqual(terminal_once, looked_up)
            self.assertEqual(_semantic_state(project), before_read_process)
            self.assertEqual(target.stat().st_mtime_ns, target_mtime)
            self.assertEqual(
                [
                    item["entry_id"]
                    for item in context["knowledge"]["active_failures"]
                ],
                [active_id],
            )
            self.assertEqual(
                context["knowledge"]["curated_markdown"],
                {
                    "memory": ["memory/README.md"],
                    "skills": ["skills/curated.md"],
                },
            )
            self.assertEqual(
                context["knowledge"]["runtime_jsonl"],
                {
                    "classification": "local_runtime",
                    "recommended_ignore_patterns": [
                        "memory/**/*.jsonl",
                        "skills/**/*.jsonl",
                    ],
                },
            )
            serialized_context = json.dumps(context, sort_keys=True)
            self.assertNotIn("expired installed failure", serialized_context)
            self.assertNotIn("one terminal effect", json.dumps(terminal_once))
            self.assertNotIn("one terminal effect", serialized_context)
            self.assertNotIn("external-terminal-action", serialized_context)
            self.assertTrue(
                all(
                    set(item) == {"event_id", "event_type", "timestamp"}
                    for item in context["ledger_tail"]
                )
            )

            audit = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import json; from causality.ledger import EvidenceLedger; "
                    "ledger=EvidenceLedger('.causality/ledger.jsonl'); "
                    "print(json.dumps({'chain':ledger.verify_chain(),"
                    "'count':ledger.event_count()}))",
                ],
                cwd=project,
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(audit.returncode, 0, audit.stderr)
            self.assertTrue(json.loads(audit.stdout)["chain"])
            self.assertEqual(_tree_snapshot(repo_build), build_before)


if __name__ == "__main__":
    unittest.main()
