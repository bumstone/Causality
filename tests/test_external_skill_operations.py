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
import test_mcp_external as external_support


class ExternalSkillOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request_id = 1

    def _finish_server(self, process: subprocess.Popen[str]) -> None:
        external_support.ExternalMCPTests._finish_server(self, process)

    def _call(
        self,
        process: subprocess.Popen[str],
        name: str,
        arguments: dict[str, Any],
        *,
        expect_error: bool = False,
    ) -> dict[str, Any]:
        request = external_support.ExternalMCPTests._request(
            self.request_id, name, arguments
        )
        self.request_id += 1
        return external_support.ExternalMCPTests._exchange(
            self, process, request, expect_error=expect_error
        )

    @staticmethod
    def _begin(key: str, verification_argv: list[str]) -> dict[str, Any]:
        return {
            "objective": "exercise durable external skill lifecycle",
            "risk": "low",
            "permissions": {
                "allowed_tools": ["shell"],
                "write_scope": [],
                "network_scope": [],
                "auth_scope": [],
            },
            "verification_requirements": [
                {
                    "id": "skill-pass",
                    "argv": verification_argv,
                    "expected_exit_codes": [0],
                    "timeout_seconds": 30,
                    "artifact_paths": {},
                    "required": True,
                    "manual": False,
                }
            ],
            "stop_condition": {
                "max_iterations": 8,
                "max_failed_hypotheses": 3,
                "no_progress_iterations": 2,
            },
            "non_goals": ["write outside the external fixture"],
            "idempotency_key": key,
        }

    def _verified_task(
        self,
        process: subprocess.Popen[str],
        suffix: str,
        verification_argv: list[str],
        *,
        reflect: bool = False,
    ) -> tuple[str, str, dict[str, Any] | None]:
        begun = self._call(
            process,
            "causality_task_begin",
            self._begin(f"begin-{suffix}", verification_argv),
        )
        task_id = begun["task"]["task_id"]
        verified = self._call(
            process,
            "causality_task_verify",
            {
                "task_id": task_id,
                "idempotency_key": f"verify-{suffix}",
                "requirement_id": "skill-pass",
                "mode": "execute",
            },
        )
        evidence_hash = verified["event_hash"]
        for index, verifier in enumerate(("correctness", "evidence"), 1):
            self._call(
                process,
                "causality_task_verdict",
                {
                    "task_id": task_id,
                    "idempotency_key": f"verdict-{suffix}-{index}",
                    "verifier": f"external-{verifier}-{suffix}",
                    "status": "pass",
                    "rationale": "independent installed-project verification",
                    "severity": "normal",
                    "evidence_refs": [evidence_hash],
                },
            )
        completed = self._call(
            process,
            "causality_task_complete",
            {"task_id": task_id, "idempotency_key": f"complete-{suffix}"},
        )
        self.assertEqual(completed["task"]["state"], "verified")
        reflection = None
        if reflect:
            reflection = self._call(
                process,
                "causality_task_reflect",
                {"task_id": task_id, "idempotency_key": f"reflect-{suffix}"},
            )
        return task_id, evidence_hash, reflection

    def test_installed_stdio_skill_loop_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            environment = base / "venv"
            project = base / "external-project"
            package_source = base / "package-source"
            project.mkdir()
            package_source.mkdir()
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy2(external_support.REPO_ROOT / name, package_source / name)
            shutil.copytree(
                external_support.SRC_ROOT,
                package_source / "src",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

            venv.EnvBuilder(with_pip=True).create(environment)
            python = environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            env = {
                name: value
                for name, value in os.environ.items()
                if name != "PYTHONPATH" and not name.startswith("CAUSALITY_")
            }
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            verification_argv = [str(python), "-c", "print('skill-pass')"]
            env["CAUSALITY_VERIFICATION_COMMANDS_JSON"] = json.dumps(
                [verification_argv]
            )
            env["CAUSALITY_APPROVAL_TOKEN"] = "trusted"
            installed = subprocess.run(
                [
                    str(python), "-m", "pip", "install",
                    "--disable-pip-version-check", "--no-input", "--no-deps",
                    str(package_source),
                ],
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            imported = subprocess.run(
                [
                    str(python), "-c",
                    "from pathlib import Path; import causality; "
                    "print(Path(causality.__file__).resolve())",
                ],
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertTrue(
                Path(imported.stdout.strip()).is_relative_to(environment.resolve())
            )

            attempts: list[tuple[str, str]] = []
            with external_support.ExternalMCPTests._server(
                self, python, project, env
            ) as process:
                task1, ref1, reflection = self._verified_task(
                    process, "one", verification_argv, reflect=True
                )
                assert reflection is not None
                skill_id = reflection["data"]["skill"]["skill_id"]
                attempts.append((task1, ref1))
                attempts.append(self._verified_task(process, "two", verification_argv)[:2])
                attempts.append(self._verified_task(process, "three", verification_argv)[:2])
                for index, (task_id, evidence_hash) in enumerate(attempts, 1):
                    self._call(
                        process,
                        "causality_skill_outcome",
                        {
                            "task_id": task_id,
                            "idempotency_key": f"outcome-{index}",
                            "skill_id": skill_id,
                            "success": True,
                            "evidence_refs": [evidence_hash],
                        },
                    )

            with external_support.ExternalMCPTests._server(
                self, python, project, env
            ) as restarted:
                promoted = self._call(
                    restarted,
                    "causality_skill_promote",
                    {
                        "skill_id": skill_id,
                        "idempotency_key": "promote-installed",
                        "approved_by": "operator",
                        "evidence_refs": [ref for _, ref in attempts],
                        "proof": "trusted",
                    },
                )
                self.assertEqual(promoted["skill"]["attempts"], 3)
                recalled = self._call(
                    restarted,
                    "causality_skill_recall",
                    {
                        "objective": "durable external skill lifecycle",
                        "limit": 10,
                    },
                )
                self.assertIn(
                    skill_id,
                    {item["skill_id"] for item in recalled["skills"]},
                )
                self.assertNotIn("trusted", json.dumps(promoted))


if __name__ == "__main__":
    unittest.main()
