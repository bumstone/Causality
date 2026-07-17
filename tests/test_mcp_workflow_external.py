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
APPROVAL_TOKEN = "external-workflow-approval-secret"
PHASES = (
    "root-cause-protocol/reproduce",
    "root-cause-protocol/hypothesis",
    "root-cause-protocol/verify",
    "root-cause-protocol/fix",
)


class ExternalWorkflowMCPTests(unittest.TestCase):
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
        expect_error: bool = False,
    ) -> dict[str, Any]:
        payload = self._exchange(
            process,
            self._request(self.request_id, name, arguments),
            timeout=timeout,
            expect_error=expect_error,
        )
        self.request_id += 1
        return payload

    @staticmethod
    def _ledger_count(project: Path) -> int:
        return sum(
            1
            for path in (project / ".causality").glob("ledger*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    def _start_phase(
        self,
        process: subprocess.Popen[str],
        task_id: str,
        phase_id: str,
        suffix: str,
    ) -> dict[str, Any]:
        return self._call(
            process,
            "causality_task_phase",
            {
                "task_id": task_id,
                "idempotency_key": f"{suffix}-start",
                "phase_id": phase_id,
                "action": "start",
            },
        )

    def _finish_phase(
        self,
        process: subprocess.Popen[str],
        task_id: str,
        phase_id: str,
        suffix: str,
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        return self._call(
            process,
            "causality_task_phase",
            {
                "task_id": task_id,
                "idempotency_key": f"{suffix}-finish",
                "phase_id": phase_id,
                "action": "finish",
                "status": "passed",
                "evidence_refs": evidence_refs,
            },
        )

    def _action(
        self,
        process: subprocess.Popen[str],
        task_id: str,
        suffix: str,
        content: str,
    ) -> str:
        response = self._call(
            process,
            "causality_task_action",
            {
                "task_id": task_id,
                "idempotency_key": f"{suffix}-action",
                "action": {
                    "kind": "file_write",
                    "path": "out/workflow.txt",
                    "content": content,
                },
            },
        )
        return response["event_hash"]

    def _verification_bundle(
        self,
        process: subprocess.Popen[str],
        task_id: str,
        suffix: str,
    ) -> list[str]:
        verified = self._call(
            process,
            "causality_task_verify",
            {
                "task_id": task_id,
                "idempotency_key": f"{suffix}-verify",
                "requirement_id": "workflow-acceptance",
                "mode": "execute",
            },
            timeout=90,
        )
        self.assertEqual(verified["data"]["status"], "pass")
        evidence_hash = verified["event_hash"]
        decision_hashes = []
        for index, role in enumerate(("correctness", "evidence"), start=1):
            verdict = self._call(
                process,
                "causality_task_verdict",
                {
                    "task_id": task_id,
                    "idempotency_key": f"{suffix}-{role}-verdict",
                    "verifier": f"external-{suffix}-{role}",
                    "status": "pass",
                    "rationale": f"{role} verified phase {suffix}",
                    "evidence_refs": [evidence_hash],
                },
            )
            self.assertEqual(verdict["data"]["decision"]["status"], "pass")
            decision_hashes.append(verdict["data"]["decision_event_hash"])
        return [evidence_hash, *decision_hashes]

    def test_installed_workflow_debug_loop_survives_restart_and_reflects_once(
        self,
    ) -> None:
        repo_build = REPO_ROOT / "build"
        build_before = _tree_snapshot(repo_build)
        self.addCleanup(
            lambda: self.assertEqual(
                _tree_snapshot(repo_build),
                build_before,
                "external package installation polluted the repository build tree",
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            environment = base / "fresh venv"
            project = base / "external project"
            package_source = base / "package source"
            project.mkdir()
            package_source.mkdir()
            (project / "test_workflow_progress.py").write_text(
                "import unittest\n"
                "from pathlib import Path\n\n"
                "class WorkflowAcceptance(unittest.TestCase):\n"
                "    def test_external_state(self):\n"
                "        value = Path('out/workflow.txt').read_text(encoding='utf-8')\n"
                "        self.assertIn(value, {\n"
                "            'reproduced\\n', 'candidate-1\\n', 'candidate-2\\n',\n"
                "            'candidate-3\\n', 'supported\\n', 'fixed\\n',\n"
                "        })\n",
                encoding="utf-8",
            )
            (project / "test_workflow_target.py").write_text(
                "import unittest\n"
                "from pathlib import Path\n\n"
                "class WorkflowTarget(unittest.TestCase):\n"
                "    def test_fixed_target(self):\n"
                "        self.assertEqual(\n"
                "            Path('out/workflow.txt').read_text(encoding='utf-8'),\n"
                "            'fixed\\n',\n"
                "        )\n",
                encoding="utf-8",
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

            verification_argv = [
                str(python),
                "-m",
                "unittest",
                "discover",
                "-s",
                ".",
                "-p",
                "test_workflow_progress.py",
                "-v",
            ]
            target_argv = [
                str(python),
                "-m",
                "unittest",
                "discover",
                "-s",
                ".",
                "-p",
                "test_workflow_target.py",
                "-v",
            ]
            server_env = dict(clean_env)
            server_env.update(
                {
                    "CAUSALITY_VERIFICATION_COMMANDS_JSON": json.dumps(
                        [verification_argv, target_argv]
                    ),
                    "CAUSALITY_APPROVAL_TOKEN": APPROVAL_TOKEN,
                }
            )
            begin = {
                "objective": "debug an installed project through the durable loop",
                "summary": "external workflow acceptance",
                "risk": "low",
                "permissions": {
                    "allowed_tools": ["file.write", "shell"],
                    "write_scope": ["out"],
                    "network_scope": [],
                    "auth_scope": [],
                },
                "verification_requirements": [
                    {
                        "id": "workflow-acceptance",
                        "argv": verification_argv,
                        "expected_exit_codes": [0],
                        "timeout_seconds": 60,
                        "artifact_paths": {},
                        "required": True,
                        "manual": False,
                    },
                    {
                        "id": "fixed-target",
                        "argv": target_argv,
                        "expected_exit_codes": [0],
                        "timeout_seconds": 60,
                        "artifact_paths": {},
                        "required": False,
                        "manual": False,
                    },
                ],
                "stop_condition": {
                    "max_iterations": 8,
                    "max_failed_hypotheses": 3,
                    "no_progress_iterations": 2,
                },
                "non_goals": ["write outside the external project"],
                "workflow": "root-cause-protocol",
                "idempotency_key": "external-workflow-begin",
            }

            self.request_id = 1
            rejected_hashes = []
            third_hypothesis: dict[str, Any] | None = None
            first_failure_hash = ""
            with self._server(python, project, server_env) as process:
                begun = self._call(process, "causality_task_begin", begin)
                task_id = begun["task"]["task_id"]
                self.assertEqual(begun["task"]["workflow"], "root-cause-protocol")
                self.assertEqual(
                    [
                        phase["phase_id"]
                        for phase in begun["task"]["workflow_phases"]
                    ],
                    list(PHASES),
                )

                self._start_phase(process, task_id, PHASES[0], "reproduce")
                action_hash = self._action(
                    process, task_id, "reproduce", "reproduced\n"
                )
                before_denied_finish = self._ledger_count(project)
                denied_finish = self._call(
                    process,
                    "causality_task_phase",
                    {
                        "task_id": task_id,
                        "idempotency_key": "reproduce-premature-finish",
                        "phase_id": PHASES[0],
                        "action": "finish",
                        "status": "passed",
                        "evidence_refs": [action_hash],
                    },
                    expect_error=True,
                )
                self.assertEqual(
                    denied_finish["error"]["code"], "phase_evidence_incomplete"
                )
                self.assertEqual(denied_finish["task"]["current_phase_id"], PHASES[0])
                self.assertEqual(
                    denied_finish["task"]["workflow_phases"][0]["status"],
                    "running",
                )
                self.assertEqual(
                    self._ledger_count(project), before_denied_finish
                )
                reproduce_evidence = [
                    action_hash,
                    *self._verification_bundle(process, task_id, "reproduce"),
                ]
                reproduced = self._finish_phase(
                    process,
                    task_id,
                    PHASES[0],
                    "reproduce",
                    reproduce_evidence,
                )
                self.assertEqual(reproduced["task"]["current_phase_id"], PHASES[1])
                self._start_phase(process, task_id, PHASES[1], "hypothesis-1")

                for index in range(1, 4):
                    action_hash = self._action(
                        process,
                        task_id,
                        f"rejected-{index}",
                        f"candidate-{index}\n",
                    )
                    if index == 1:
                        target_failure = self._call(
                            process,
                            "causality_task_verify",
                            {
                                "task_id": task_id,
                                "idempotency_key": "fixed-target-failure",
                                "requirement_id": "fixed-target",
                                "mode": "execute",
                            },
                            timeout=90,
                        )
                        self.assertEqual(target_failure["data"]["status"], "fail")
                        failure_verification = self._call(
                            process,
                            "causality_task_verify",
                            {
                                "task_id": task_id,
                                "idempotency_key": "duplicate-failure-verify",
                                "requirement_id": "workflow-acceptance",
                                "mode": "execute",
                            },
                            timeout=90,
                        )
                        self.assertEqual(
                            failure_verification["data"]["status"], "pass"
                        )
                        for duplicate, (verifier, rationale) in enumerate(
                            (
                                ("Root Cause Reviewer", "Same   failure cause"),
                                (" root cause reviewer ", " same failure cause "),
                            ),
                            start=1,
                        ):
                            failure = self._call(
                                process,
                                "causality_task_verdict",
                                {
                                    "task_id": task_id,
                                    "idempotency_key": f"duplicate-failure-{duplicate}",
                                    "verifier": verifier,
                                    "status": "fail",
                                    "rationale": rationale,
                                    "evidence_refs": [target_failure["event_hash"]],
                                },
                            )
                            if duplicate == 1:
                                first_failure_hash = failure["data"][
                                    "decision_event_hash"
                                ]
                    hypothesis = {
                        "task_id": task_id,
                        "idempotency_key": f"rejected-hypothesis-{index}",
                        "phase_id": PHASES[1],
                        "hypothesis": f"candidate cause {index}",
                        "verifier": f"external-debugger-{index}",
                        "status": "rejected",
                        "rationale": f"experiment {index} disproved the cause",
                        "evidence_refs": [action_hash],
                    }
                    rejected = self._call(
                        process,
                        "causality_task_hypothesis",
                        hypothesis,
                    )
                    rejected_hashes.append(rejected["event_hash"])
                    third_hypothesis = hypothesis

                approval_refs = rejected["task"]["approval_evidence_refs"]
                self.assertEqual(rejected["task"]["state"], "blocked")
                self.assertEqual(approval_refs[:3], rejected_hashes)
                self.assertEqual(len(approval_refs), 4)
                third_event_hash = rejected["event_hash"]
                blocked_count = self._ledger_count(project)

            assert third_hypothesis is not None
            with self._server(python, project, server_env) as process:
                replayed = self._call(
                    process,
                    "causality_task_hypothesis",
                    third_hypothesis,
                )
                self.assertEqual(
                    replayed["idempotency"],
                    {"key": "rejected-hypothesis-3", "replayed": True},
                )
                self.assertEqual(replayed["event_hash"], third_event_hash)
                self.assertEqual(
                    replayed["task"]["approval_evidence_refs"], approval_refs
                )
                self.assertEqual(self._ledger_count(project), blocked_count)
                approved = self._call(
                    process,
                    "causality_task_approve",
                    {
                        "task_id": task_id,
                        "idempotency_key": "external-phase-approval",
                        "stage": "phase",
                        "phase_id": PHASES[1],
                        "approved": True,
                        "approver": "external-operator",
                        "rationale": "reviewed the bounded rejection streak",
                        "evidence_refs": approval_refs,
                        "proof": APPROVAL_TOKEN,
                    },
                )
                self.assertEqual(approved["task"]["state"], "executing")
                self.assertEqual(approved["task"]["workflow_phases"][1]["status"], "failed")

                restarted = self._start_phase(
                    process, task_id, PHASES[1], "hypothesis-2"
                )
                self.assertEqual(restarted["task"]["workflow_phases"][1]["attempt"], 2)
                supported_action = self._action(
                    process, task_id, "supported", "supported\n"
                )
                supported = self._call(
                    process,
                    "causality_task_hypothesis",
                    {
                        "task_id": task_id,
                        "idempotency_key": "supported-hypothesis",
                        "phase_id": PHASES[1],
                        "hypothesis": "verified root cause",
                        "verifier": "external-debugger-supported",
                        "status": "supported",
                        "rationale": "experiment confirmed the root cause",
                        "evidence_refs": [supported_action],
                    },
                )
                hypothesis_evidence = [
                    supported_action,
                    supported["event_hash"],
                    *self._verification_bundle(process, task_id, "hypothesis-2"),
                ]
                self._finish_phase(
                    process,
                    task_id,
                    PHASES[1],
                    "hypothesis-2",
                    hypothesis_evidence,
                )

                self._start_phase(process, task_id, PHASES[2], "verify")
                verify_evidence = self._verification_bundle(
                    process, task_id, "verify"
                )
                self._finish_phase(
                    process, task_id, PHASES[2], "verify", verify_evidence
                )

                self._start_phase(process, task_id, PHASES[3], "fix")
                fix_action = self._action(process, task_id, "fix", "fixed\n")
                target_success = self._call(
                    process,
                    "causality_task_verify",
                    {
                        "task_id": task_id,
                        "idempotency_key": "fixed-target-success",
                        "requirement_id": "fixed-target",
                        "mode": "execute",
                    },
                    timeout=90,
                )
                self.assertEqual(target_success["data"]["status"], "pass")
                fix_evidence = [
                    fix_action,
                    target_success["event_hash"],
                    *self._verification_bundle(process, task_id, "fix"),
                ]
                finished = self._finish_phase(
                    process, task_id, PHASES[3], "fix", fix_evidence
                )
                self.assertIsNone(finished["task"]["current_phase_id"])
                self.assertTrue(
                    all(
                        phase["status"] == "passed"
                        for phase in finished["task"]["workflow_phases"]
                    )
                )

                complete = {
                    "task_id": task_id,
                    "idempotency_key": "external-workflow-complete",
                }
                completed = self._call(
                    process, "causality_task_complete", complete
                )
                self.assertEqual(completed["task"]["state"], "verified")
                completed_count = self._ledger_count(project)
                completed_replay = self._call(
                    process, "causality_task_complete", complete
                )
                self.assertTrue(completed_replay["idempotency"]["replayed"])
                self.assertEqual(
                    completed_replay["event_hash"], completed["event_hash"]
                )
                self.assertEqual(completed_replay["task"], completed["task"])
                self.assertEqual(self._ledger_count(project), completed_count)
                reflect = {
                    "task_id": task_id,
                    "idempotency_key": "external-workflow-reflect",
                    "scope": "external-workflow",
                    "ttl_days": 30,
                }
                reflected = self._call(
                    process, "causality_task_reflect", reflect
                )
                reflected_replay = self._call(
                    process, "causality_task_reflect", reflect
                )
                self.assertEqual(len(reflected["data"]["failures"]), 1)
                self.assertTrue(reflected_replay["idempotency"]["replayed"])
                self.assertEqual(
                    reflected_replay["event_hash"], reflected["event_hash"]
                )

            self.assertEqual(
                (project / "out" / "workflow.txt").read_text(encoding="utf-8"),
                "fixed\n",
            )
            audit_script = "\n".join(
                (
                    "import json, sys",
                    "from pathlib import Path",
                    "from causality.ledger import EvidenceLedger",
                    "from causality.memory import TypedMemory",
                    "from causality.task_lifecycle import TaskLifecycle",
                    "root = Path(sys.argv[1]).resolve()",
                    "task_id = sys.argv[2]",
                    "ledger = EvidenceLedger(root / '.causality' / 'ledger.jsonl')",
                    "task = TaskLifecycle(root).get(task_id)",
                    "memory = TypedMemory(root)",
                    "failures = memory.entries('failures')",
                    "retrospectives = memory.entries('retrospectives')",
                    "print(json.dumps({",
                    "'chain': ledger.verify_chain(),",
                    "'task': task.to_dict(),",
                    "'attempts': [phase.attempt for phase in task.workflow_phases],",
                    "'statuses': [phase.status for phase in task.workflow_phases],",
                    "'failure_count': len(failures),",
                    "'retrospective_count': len(retrospectives),",
                    "'failure': failures[0].to_dict() if failures else None,",
                    "}))",
                )
            )
            audited = subprocess.run(
                [str(python), "-c", audit_script, str(project), task_id],
                cwd=project,
                env=clean_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(audited.returncode, 0, audited.stderr)
            audit = json.loads(audited.stdout)
            self.assertIs(audit["chain"], True)
            self.assertEqual(audit["task"]["state"], "verified")
            self.assertTrue(audit["task"]["terminal"])
            self.assertEqual(audit["task"]["hypothesis_count"], 3)
            self.assertEqual(audit["attempts"], [1, 2, 1, 1])
            self.assertEqual(audit["statuses"], ["passed"] * 4)
            self.assertEqual(audit["failure_count"], 1)
            self.assertEqual(audit["retrospective_count"], 1)
            self.assertEqual(audit["failure"]["provenance"], first_failure_hash)
            self.assertEqual(
                audit["failure"]["metadata"],
                {
                    "phase_id": PHASES[1],
                    "scope": "external-workflow",
                    "ttl_days": 30,
                },
            )
            ledger_text = (project / ".causality" / "ledger.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertNotIn(APPROVAL_TOKEN, ledger_text)


if __name__ == "__main__":
    unittest.main()
