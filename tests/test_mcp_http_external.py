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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
QUERY_SECRET = "query-secret-004a"
HEADER_VALUE = "header-value-004a"
BODY_SECRET = b"body-secret-004a\n"
CREDENTIAL_SECRET = "Bearer credential-secret-004a"
APPROVAL_TOKEN = "approval-secret-004a"
RESPONSE_BODY = b"accepted response\n"
RESPONSE_SHA256 = hashlib.sha256(RESPONSE_BODY).hexdigest()


def _tree_snapshot(root: Path) -> tuple[tuple[str, str], ...] | None:
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


@contextmanager
def _http_endpoint() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0"))
            requests.append(
                {
                    "path": self.path,
                    "request_metadata": self.headers.get("X-Request-Metadata"),
                    "authorization": self.headers.get("Authorization"),
                    "body": self.rfile.read(length),
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(RESPONSE_BODY)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(RESPONSE_BODY)

        def log_message(self, _format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class ExternalHttpMCPTests(unittest.TestCase):
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

        self.assertTrue(line.endswith("\n"), f"MCP returned no JSON line: {line!r}")
        response = json.loads(line)
        self.assertEqual(response.get("jsonrpc"), "2.0")
        self.assertEqual(response.get("id"), request["id"])
        self.assertNotIn("error", response, response)
        result = response["result"]
        self.assertFalse(result.get("isError", False), result)
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
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(return_code, 0, stderr)
        self.assertEqual(stdout, "", f"unexpected non-response stdout: {stdout!r}")
        self.assertEqual(stderr, "", f"unexpected MCP stderr: {stderr!r}")

    @contextmanager
    def _server(
        self,
        python: Path,
        project: Path,
        environment: dict[str, str],
    ) -> Iterator[subprocess.Popen[str]]:
        process = subprocess.Popen(
            [str(python), "-m", "causality.mcp_server", "--project", str(project)],
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

    def test_installed_http_action_is_scoped_secret_safe_and_exactly_once(self) -> None:
        repo_build = REPO_ROOT / "build"
        build_before = _tree_snapshot(repo_build)
        self.addCleanup(
            lambda: self.assertEqual(
                _tree_snapshot(repo_build),
                build_before,
                "external package installation polluted the repository build tree",
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir, _http_endpoint() as (
            origin,
            requests,
        ):
            base = Path(temp_dir)
            environment = base / "fresh venv"
            project = base / "external project"
            package_source = base / "package source"
            io_dir = project / "io"
            io_dir.mkdir(parents=True)
            (io_dir / "request.bin").write_bytes(BODY_SECRET)
            (project / "test_http_acceptance.py").write_text(
                "\n".join(
                    (
                        "import hashlib",
                        "import unittest",
                        "from pathlib import Path",
                        "",
                        "class HttpAcceptanceTest(unittest.TestCase):",
                        "    def test_response_artifact(self):",
                        "        body = Path('io/response.bin').read_bytes()",
                        f"        self.assertEqual(body, {RESPONSE_BODY!r})",
                        f"        self.assertEqual(hashlib.sha256(body).hexdigest(), '{RESPONSE_SHA256}')",
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
            clean_env.update(
                {
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "CAUSALITY_NETWORK_ORIGINS_JSON": json.dumps([origin]),
                    "CAUSALITY_AUTH_REFS_JSON": json.dumps(["external-api"]),
                    "CAUSALITY_HTTP_HEADERS_JSON": json.dumps(
                        ["Content-Type", "X-Request-Metadata"]
                    ),
                    "CAUSALITY_HTTP_CREDENTIALS_JSON": json.dumps(
                        {"external-api": {"Authorization": CREDENTIAL_SECRET}}
                    ),
                    "CAUSALITY_APPROVAL_TOKEN": APPROVAL_TOKEN,
                }
            )
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
            self.assertFalse(import_path.is_relative_to(REPO_ROOT.resolve()))

            verification_argv = [
                str(python),
                "-m",
                "unittest",
                "-v",
                "test_http_acceptance",
            ]
            begin_arguments = {
                "objective": "execute one installed, scoped HTTP task",
                "summary": "external HTTP stdio acceptance",
                "risk": "low",
                "permissions": {
                    "allowed_tools": ["http", "shell"],
                    "write_scope": ["io"],
                    "network_scope": [origin],
                    "auth_scope": ["external-api"],
                },
                "verification_requirements": [
                    {
                        "id": "external-http",
                        "argv": verification_argv,
                        "expected_exit_codes": [0],
                        "timeout_seconds": 30,
                        "artifact_paths": {"io/response.bin": RESPONSE_SHA256},
                        "required": True,
                        "manual": False,
                    }
                ],
                "stop_condition": {
                    "max_iterations": 8,
                    "max_failed_hypotheses": 3,
                    "no_progress_iterations": 2,
                },
                "non_goals": ["send outside the declared origin"],
                "idempotency_key": "external-http-begin",
            }
            action_arguments = {
                "task_id": "",
                "idempotency_key": "external-http-action",
                "method": "POST",
                "url": f"{origin}/submit?token={QUERY_SECRET}",
                "headers": {
                    "Content-Type": "application/octet-stream",
                    "X-Request-Metadata": HEADER_VALUE,
                },
                "body_ref": "io/request.bin",
                "timeout_seconds": 10,
                "expected_statuses": [200],
                "response_artifact": "io/response.bin",
                "auth_ref": "external-api",
            }

            with self._server(python, project, clean_env) as first:
                begun = self._exchange(
                    first,
                    self._request(1, "causality_task_begin", begin_arguments),
                )
                task_id = begun["task"]["task_id"]
                action_arguments["task_id"] = task_id
                approved = self._exchange(
                    first,
                    self._request(
                        2,
                        "causality_task_approve",
                        {
                            "task_id": task_id,
                            "idempotency_key": "external-send-approval",
                            "stage": "external_send",
                            "approved": True,
                            "approver": "external-human",
                            "rationale": "approve the bounded local HTTP send",
                            "evidence_refs": [],
                            "proof": APPROVAL_TOKEN,
                        },
                    ),
                )
                self.assertEqual(approved["data"]["stage"], "external_send")
                acted = self._exchange(
                    first,
                    self._request(3, "causality_task_http", action_arguments),
                )
                self.assertEqual(
                    acted["idempotency"],
                    {"key": "external-http-action", "replayed": False},
                )
                self.assertEqual(acted["data"]["status"], 200)
                self.assertTrue(acted["data"]["artifact_written"])
                self.assertEqual(acted["data"]["response_sha256"], RESPONSE_SHA256)

            self.assertEqual(
                requests,
                [
                    {
                        "path": f"/submit?token={QUERY_SECRET}",
                        "request_metadata": HEADER_VALUE,
                        "authorization": CREDENTIAL_SECRET,
                        "body": BODY_SECRET,
                    }
                ],
            )
            artifact = io_dir / "response.bin"
            self.assertEqual(artifact.read_bytes(), RESPONSE_BODY)
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), RESPONSE_SHA256)
            os.utime(artifact, (1, 1))
            artifact_mtime = artifact.stat().st_mtime_ns

            with self._server(python, project, clean_env) as restarted:
                replayed = self._exchange(
                    restarted,
                    self._request(4, "causality_task_http", action_arguments),
                )
                self.assertEqual(
                    replayed["idempotency"],
                    {"key": "external-http-action", "replayed": True},
                )
                self.assertEqual(replayed["event_hash"], acted["event_hash"])
                self.assertEqual(replayed["data"], acted["data"])
                self.assertEqual(len(requests), 1)
                self.assertEqual(artifact.stat().st_mtime_ns, artifact_mtime)

                verified = self._exchange(
                    restarted,
                    self._request(
                        5,
                        "causality_task_verify",
                        {
                            "task_id": task_id,
                            "idempotency_key": "external-http-verify",
                            "requirement_id": "external-http",
                            "mode": "execute",
                        },
                    ),
                )
                self.assertEqual(verified["data"]["status"], "pass")
                evidence_hash = verified["event_hash"]

                for request_id, verifier in enumerate(
                    ("external-http-security", "external-http-conformance"), start=6
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
                                "rationale": f"{verifier} checked the evidence",
                                "severity": "normal",
                                "evidence_refs": [evidence_hash],
                            },
                        ),
                    )
                    self.assertEqual(verdict["data"]["decision"]["status"], "pass")

                completed = self._exchange(
                    restarted,
                    self._request(
                        8,
                        "causality_task_complete",
                        {
                            "task_id": task_id,
                            "idempotency_key": "external-http-complete",
                        },
                    ),
                )
                self.assertEqual(completed["data"]["decision"], "pass")
                self.assertEqual(completed["task"]["state"], "verified")
                self.assertTrue(completed["task"]["terminal"])

            ledger = project / ".causality" / "ledger.jsonl"
            ledger_text = ledger.read_text(encoding="utf-8")
            for secret in (
                QUERY_SECRET,
                HEADER_VALUE,
                BODY_SECRET.decode().strip(),
                CREDENTIAL_SECRET,
                APPROVAL_TOKEN,
            ):
                self.assertNotIn(secret, ledger_text)

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
                    "'intent': sum(e.event_type == 'task_action_intent' and "
                    "e.payload.get('idempotency_key') == 'external-http-action' for e in events),",
                    "'result': sum(e.event_type == 'task_action_result' and "
                    "e.payload.get('idempotency_key') == 'external-http-action' for e in events),",
                    "'http_tool': sum(e.event_type == 'tool_call' and "
                    "e.payload.get('tool') == 'http' for e in events),",
                    "}",
                    "print(json.dumps({'chain': ledger.verify_chain(), 'task': task, "
                    "'counts': counts}))",
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
            self.assertEqual(audit["counts"], {"intent": 1, "result": 1, "http_tool": 1})
            self.assertEqual(_tree_snapshot(repo_build), build_before)


if __name__ == "__main__":
    unittest.main()
