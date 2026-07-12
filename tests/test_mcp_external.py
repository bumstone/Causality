from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import unittest
import venv
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"


def _tree_snapshot(root: Path) -> tuple[tuple[str, str], ...] | None:
    """Capture enough content to prove an external install did not touch a tree."""

    if not root.exists():
        return None
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append((relative + "/", "directory"))
        elif path.is_file():
            entries.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(entries)


class ExternalMCPTests(unittest.TestCase):
    @staticmethod
    def _request(request_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }

    def _exchange(
        self,
        process: subprocess.Popen[str],
        request: dict[str, Any],
        *,
        timeout: float = 45,
    ) -> dict[str, Any]:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
        process.stdin.flush()

        received: queue.Queue[str] = queue.Queue(maxsize=1)
        reader = threading.Thread(
            target=lambda: received.put(process.stdout.readline()), daemon=True
        )
        reader.start()
        try:
            line = received.get(timeout=timeout)
        except queue.Empty:
            process.kill()
            self.fail(f"MCP server did not answer request {request['id']} within {timeout}s")
        reader.join(timeout=1)

        self.assertTrue(line.endswith("\n"), "MCP response must be one JSON line")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            self.fail(f"MCP stdout contained non-JSON output: {line!r} ({exc})")
        self.assertIsInstance(response, dict)
        self.assertEqual(response.get("jsonrpc"), "2.0")
        self.assertEqual(response.get("id"), request["id"])
        self.assertNotIn("error", response, response)

        result = response["result"]
        self.assertFalse(result.get("isError", False), result)
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["type"], "text")
        payload = json.loads(result["content"][0]["text"])
        self.assertIs(payload.get("ok"), True, payload)
        return payload

    def _finish_server(self, process: subprocess.Popen[str]) -> None:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.close()
        process.stdin = None
        try:
            return_code = process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            self.fail("MCP server did not exit after stdin reached EOF")
        try:
            extra_stdout = process.stdout.read()
            stderr = process.stderr.read()
        finally:
            process.stdout.close()
            process.stderr.close()
        self.assertEqual(return_code, 0, stderr)
        self.assertEqual(extra_stdout, "", f"unexpected non-response stdout: {extra_stdout!r}")
        self.assertEqual(stderr, "", f"unexpected MCP stderr: {stderr!r}")

    @contextmanager
    def _server(
        self, python: Path, project: Path, environment: dict[str, str]
    ) -> Iterator[subprocess.Popen[str]]:
        process = subprocess.Popen(
            [
                str(python),
                "-m",
                "causality.mcp_server",
                "--project",
                str(project),
            ],
            cwd=project,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                self._finish_server(process)

    def test_installed_package_stdio_lifecycle_survives_restart_exactly_once(self) -> None:
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
            (project / "test_external_effect.py").write_text(
                "\n".join(
                    (
                        "import unittest",
                        "from pathlib import Path",
                        "",
                        "",
                        "class ExternalEffectTest(unittest.TestCase):",
                        "    def test_effect(self):",
                        "        self.assertEqual(",
                        "            Path('out/effect.txt').read_text(encoding='utf-8'),",
                        "            'one durable effect\\n',",
                        "        )",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            package_source.mkdir()
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
            clean_env = os.environ.copy()
            clean_env.pop("PYTHONPATH", None)
            for name in (
                "CAUSALITY_SUBPROCESS_PREFIXES_JSON",
                "CAUSALITY_VERIFICATION_COMMANDS_JSON",
                "CAUSALITY_VERIFICATION_PREFIXES_JSON",
                "CAUSALITY_NETWORK_ORIGINS_JSON",
                "CAUSALITY_AUTH_REFS_JSON",
                "CAUSALITY_HTTP_HEADERS_JSON",
                "CAUSALITY_HTTP_CREDENTIALS_JSON",
                "CAUSALITY_APPROVAL_TOKEN",
            ):
                clean_env.pop(name, None)
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
                timeout=20,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            import_path = Path(imported.stdout.strip())
            self.assertTrue(import_path.is_relative_to(environment.resolve()), import_path)
            self.assertFalse(import_path.is_relative_to(package_source.resolve()))
            self.assertFalse(import_path.is_relative_to(REPO_ROOT.resolve()))

            verification_argv = [
                str(python),
                "-m",
                "unittest",
                "-v",
                "test_external_effect",
            ]
            begin_arguments = {
                "objective": "exercise an installed MCP lifecycle across restart",
                "summary": "external stdio acceptance",
                "risk": "low",
                "permissions": {
                    "allowed_tools": ["file.write", "shell"],
                    "write_scope": ["out"],
                    "network_scope": [],
                    "auth_scope": [],
                },
                "verification_requirements": [
                    {
                        "id": "external-effect",
                        "argv": verification_argv,
                        "expected_exit_codes": [0],
                        "timeout_seconds": 30,
                        "artifact_paths": {"out/effect.txt": None},
                        "required": True,
                        "manual": False,
                    }
                ],
                "stop_condition": {
                    "max_iterations": 8,
                    "max_failed_hypotheses": 3,
                    "no_progress_iterations": 2,
                },
                "non_goals": ["write outside the external project"],
                "idempotency_key": "external-begin",
            }
            action_arguments: dict[str, Any]
            with self._server(python, project, clean_env) as first:
                begun = self._exchange(
                    first,
                    self._request(1, "causality_task_begin", begin_arguments),
                )
                task_id = begun["task"]["task_id"]
                self.assertEqual(task_id, begun["task"]["contract_id"])
                self.assertEqual(
                    begun["idempotency"],
                    {"key": "external-begin", "replayed": False},
                )
                action_arguments = {
                    "task_id": task_id,
                    "idempotency_key": "external-action",
                    "action": {
                        "kind": "file_write",
                        "path": "out/effect.txt",
                        "content": "one durable effect\n",
                    },
                }
                acted = self._exchange(
                    first,
                    self._request(2, "causality_task_action", action_arguments),
                )
                self.assertEqual(
                    acted["idempotency"],
                    {"key": "external-action", "replayed": False},
                )

            effect = project / "out" / "effect.txt"
            self.assertEqual(effect.read_text(encoding="utf-8"), "one durable effect\n")
            effects = [path for path in (project / "out").rglob("*") if path.is_file()]
            self.assertEqual(effects, [effect])
            os.utime(effect, (1, 1))
            effect_mtime_before_retry = effect.stat().st_mtime_ns

            with self._server(python, project, clean_env) as restarted:
                replayed_action = self._exchange(
                    restarted,
                    self._request(3, "causality_task_action", action_arguments),
                )
                self.assertEqual(
                    replayed_action["idempotency"],
                    {"key": "external-action", "replayed": True},
                )
                self.assertEqual(replayed_action["event_hash"], acted["event_hash"])
                self.assertEqual(replayed_action["data"], acted["data"])
                self.assertEqual(effect.stat().st_mtime_ns, effect_mtime_before_retry)

                verified = self._exchange(
                    restarted,
                    self._request(
                        4,
                        "causality_task_verify",
                        {
                            "task_id": task_id,
                            "idempotency_key": "external-verify",
                            "requirement_id": "external-effect",
                            "mode": "execute",
                        },
                    ),
                )
                self.assertEqual(verified["data"]["status"], "pass")
                evidence_hash = verified["event_hash"]
                self.assertRegex(evidence_hash, r"^[0-9a-f]{64}$")

                for request_id, verifier in enumerate(
                    ("external-security", "external-conformance"), start=5
                ):
                    verdict = self._exchange(
                        restarted,
                        self._request(
                            request_id,
                            "causality_task_verdict",
                            {
                                "task_id": task_id,
                                "idempotency_key": f"{verifier}-verdict",
                                "verifier": verifier,
                                "status": "pass",
                                "rationale": f"{verifier} checked the installed result",
                                "severity": "normal",
                                "evidence_refs": [evidence_hash],
                            },
                        ),
                    )
                    self.assertEqual(verdict["data"]["decision"]["status"], "pass")

                completed = self._exchange(
                    restarted,
                    self._request(
                        7,
                        "causality_task_complete",
                        {
                            "task_id": task_id,
                            "idempotency_key": "external-complete",
                        },
                    ),
                )
                self.assertEqual(completed["task"]["state"], "verified")
                self.assertTrue(completed["task"]["terminal"])
                self.assertEqual(completed["data"]["decision"], "pass")

                reflect_arguments = {
                    "task_id": task_id,
                    "idempotency_key": "external-reflect",
                    "scope": f"task:{task_id}",
                    "ttl_days": 30,
                }
                reflected = self._exchange(
                    restarted,
                    self._request(8, "causality_task_reflect", reflect_arguments),
                )
                replayed_reflection = self._exchange(
                    restarted,
                    self._request(9, "causality_task_reflect", reflect_arguments),
                )
                self.assertEqual(
                    replayed_reflection["idempotency"],
                    {"key": "external-reflect", "replayed": True},
                )
                self.assertEqual(replayed_reflection["event_hash"], reflected["event_hash"])
                self.assertEqual(replayed_reflection["data"], reflected["data"])

            memory_logs = list(project.rglob("log.jsonl"))
            self.assertEqual(len(memory_logs), 1, memory_logs)
            memory_records = [
                json.loads(line)
                for line in memory_logs[0].read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(memory_records), 1)
            self.assertEqual(
                memory_records[0]["entry_id"],
                reflected["data"]["retrospective"]["entry_id"],
            )
            self.assertEqual(reflected["data"]["failures"], [])

            audit_script = "\n".join(
                (
                    "import json, sys",
                    "from pathlib import Path",
                    "from causality.ledger import EvidenceLedger",
                    "from causality.task_lifecycle import TaskLifecycle",
                    "root = Path(sys.argv[1]).resolve()",
                    "task_id = sys.argv[2]",
                    "ledger = EvidenceLedger(root / '.causality' / 'ledger.jsonl')",
                    "events = ledger.events(all_segments=True)",
                    "task = TaskLifecycle(root).get(task_id).to_dict()",
                    "counts = {",
                    "'task_action_intent': sum(event.event_type == 'task_action_intent' "
                    "and event.payload.get('idempotency_key') == 'external-action' "
                    "for event in events),",
                    "'task_action_result': sum(event.event_type == 'task_action_result' "
                    "and event.payload.get('idempotency_key') == 'external-action' "
                    "for event in events),",
                    "'task_reflection_intent': sum(event.event_type == "
                    "'task_reflection_intent' for event in events),",
                    "'task_reflected': sum(event.event_type == 'task_reflected' "
                    "for event in events),",
                    "}",
                    "print(json.dumps({'chain': ledger.verify_chain(), 'task': task, "
                    "'counts': counts, 'event_count': ledger.event_count()}))",
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
            self.assertEqual(len(audited.stdout.splitlines()), 1, audited.stdout)
            audit = json.loads(audited.stdout)
            self.assertIs(audit["chain"], True)
            self.assertEqual(audit["task"]["state"], "verified")
            self.assertTrue(audit["task"]["terminal"])
            self.assertEqual(
                audit["counts"],
                {
                    "task_action_intent": 1,
                    "task_action_result": 1,
                    "task_reflection_intent": 1,
                    "task_reflected": 1,
                },
            )
            self.assertEqual(_tree_snapshot(repo_build), build_before)


if __name__ == "__main__":
    unittest.main()
