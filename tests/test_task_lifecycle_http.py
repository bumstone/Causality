from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import AuditEventType, GoalContract, PermissionContract
from causality.ledger import EvidenceLedger
from causality.http_adapter import HttpAdapter
from causality.task_lifecycle import (
    TaskLifecycle,
    TaskLifecycleError,
    TaskPolicy,
    TaskState,
)


class HttpLifecyclePolicyTests(unittest.TestCase):
    def test_begin_enforces_server_network_and_auth_ceilings_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / ".causality" / "ledger.jsonl"
            lifecycle = TaskLifecycle(
                root,
                ledger_path,
                policy=TaskPolicy(
                    allowed_tools=frozenset({"http"}),
                    allowed_network_origins=frozenset({"https://api.example.com"}),
                    allowed_auth_refs=frozenset({"ci-token"}),
                ),
            )

            for suffix, permissions, code in (
                (
                    "origin",
                    PermissionContract(
                        allowed_tools=("http",),
                        network_scope=("https://other.example.com",),
                    ),
                    "policy_denied",
                ),
                (
                    "path",
                    PermissionContract(
                        allowed_tools=("http",),
                        network_scope=("https://api.example.com/path",),
                    ),
                    "validation_error",
                ),
                (
                    "auth",
                    PermissionContract(
                        allowed_tools=("http",),
                        network_scope=("https://api.example.com",),
                        auth_scope=("unknown-token",),
                    ),
                    "policy_denied",
                ),
            ):
                with self.subTest(suffix=suffix), self.assertRaises(
                    TaskLifecycleError
                ) as caught:
                    lifecycle.begin(
                        GoalContract("http", "scope", permissions=permissions),
                        idempotency_key=f"scope-{suffix}",
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(EvidenceLedger(ledger_path).event_count(), 0)

            task = lifecycle.begin(
                GoalContract(
                    "http",
                    "allowed scope",
                    permissions=PermissionContract(
                        allowed_tools=("http",),
                        network_scope=("https://API.EXAMPLE.COM:443",),
                        auth_scope=("ci-token",),
                    ),
                ),
                idempotency_key="scope-allowed",
            )
            self.assertEqual(
                task.contract_snapshot["permissions"]["network_scope"],
                ("https://api.example.com",),
            )

    def test_external_send_approval_is_available_without_rejecting_the_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            calls: list[dict[str, object]] = []
            lifecycle = TaskLifecycle(
                root,
                policy=TaskPolicy(
                    allowed_tools=frozenset({"shell"}),
                    subprocess_argv_prefixes=(("git", "push"),),
                ),
                approval_authorizer=lambda _who, _stage, proof: proof == "trusted",
                effect_runner=lambda descriptor: calls.append(descriptor) or {"ok": True},
            )
            task = lifecycle.begin(
                GoalContract(
                    "push",
                    "external effect",
                    permissions=PermissionContract(allowed_tools=("shell",)),
                ),
                idempotency_key="external-begin",
            )

            with self.assertRaises(TaskLifecycleError) as blocked:
                lifecycle.action(
                    task.task_id,
                    {"kind": "subprocess", "argv": ["git", "push"]},
                    idempotency_key="external-action",
                )
            self.assertEqual(blocked.exception.code, "approval_required")
            self.assertEqual(calls, [])
            self.assertEqual(
                [
                    event
                    for event in EvidenceLedger(lifecycle.ledger.path).events_for_contract(
                        task.task_id,
                        all_segments=True,
                    )
                    if event.event_type == AuditEventType.TASK_ACTION_INTENT.value
                ],
                [],
            )

            task = lifecycle.approve(
                task.task_id,
                stage="external_send",
                approved=True,
                approver="operator",
                rationale="approved outbound effect",
                idempotency_key="external-approval",
                proof="trusted",
            )
            self.assertEqual(task.state, TaskState.EXECUTING)
            completed = lifecycle.action(
                task.task_id,
                {"kind": "subprocess", "argv": ["git", "push"]},
                idempotency_key="external-action",
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(completed.state, TaskState.EXECUTING)

    def test_action_stage_rejection_is_nonterminal_and_replay_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lifecycle = TaskLifecycle(
                temp_dir,
                policy=TaskPolicy(allowed_tools=frozenset({"shell"})),
                approval_authorizer=lambda _who, _stage, proof: proof == "trusted",
            )
            task = lifecycle.begin(
                GoalContract(
                    "send",
                    "reject one action",
                    permissions=PermissionContract(allowed_tools=("shell",)),
                ),
                idempotency_key="reject-action-begin",
            )
            arguments = {
                "stage": "external_send",
                "approved": False,
                "approver": "operator",
                "rationale": "do not send",
                "idempotency_key": "reject-action",
                "proof": "trusted",
            }

            rejected = lifecycle.approve(task.task_id, **arguments)
            count = lifecycle.ledger.event_count()
            replay = lifecycle.approve(task.task_id, **arguments)

            self.assertFalse(rejected.terminal)
            self.assertEqual(rejected.state, TaskState.PLANNED)
            self.assertEqual(replay, rejected)
            self.assertEqual(lifecycle.ledger.event_count(), count)


class _HttpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        size = int(self.headers.get("Content-Length", "0"))
        self.server.calls += 1  # type: ignore[attr-defined]
        self.server.received = {  # type: ignore[attr-defined]
            "authorization": self.headers.get("Authorization"),
            "public": self.headers.get("X-Public"),
            "body": self.rfile.read(size),
            "path": self.path,
        }
        body = b"response-secret"
        self.send_response(201)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


class _HttpServer:
    def __enter__(self) -> ThreadingHTTPServer:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _HttpHandler)
        self.server.calls = 0  # type: ignore[attr-defined]
        self.server.received = {}  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _FailingOpener:
    def __init__(self) -> None:
        self.calls = 0

    def open(self, *_args: object, **_kwargs: object) -> object:
        self.calls += 1
        raise urllib.error.URLError("transport-secret")


class HttpLifecycleActionTests(unittest.TestCase):
    @staticmethod
    def _origin(server: ThreadingHTTPServer) -> str:
        host, port = server.server_address
        return f"http://{host}:{port}"

    def _lifecycle(
        self,
        root: Path,
        origin: str,
        *,
        credentials: dict[str, dict[str, str]] | None = None,
    ) -> TaskLifecycle:
        return TaskLifecycle(
            root,
            policy=TaskPolicy(
                allowed_tools=frozenset({"http"}),
                allowed_network_origins=frozenset({origin}),
                allowed_auth_refs=frozenset({"service-token"}),
                allowed_http_headers=frozenset({"x-public"}),
            ),
            approval_authorizer=lambda _who, _stage, proof: proof == "trusted",
            http_credentials=credentials
            or {"service-token": {"Authorization": "Bearer credential-secret"}},
        )

    @staticmethod
    def _contract(origin: str) -> GoalContract:
        return GoalContract(
            "http",
            "send a scoped request",
            permissions=PermissionContract(
                allowed_tools=("http",),
                write_scope=("io",),
                network_scope=(origin,),
                auth_scope=("service-token",),
            ),
        )

    def test_http_requires_external_send_approval_before_intent_or_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, _HttpServer() as server:
            root = Path(temp_dir)
            (root / "io").mkdir()
            lifecycle = self._lifecycle(root, self._origin(server))
            task = lifecycle.begin(
                self._contract(self._origin(server)), idempotency_key="http-begin"
            )

            with self.assertRaises(TaskLifecycleError) as caught:
                lifecycle.action(
                    task.task_id,
                    {
                        "kind": "http",
                        "method": "POST",
                        "url": self._origin(server) + "/send",
                        "expected_statuses": [201],
                    },
                    idempotency_key="http-send",
                )

            self.assertEqual(caught.exception.code, "approval_required")
            self.assertEqual(server.calls, 0)  # type: ignore[attr-defined]
            events = lifecycle.ledger.events_for_contract(task.task_id, all_segments=True)
            self.assertFalse(
                any(event.event_type == AuditEventType.TASK_ACTION_INTENT.value for event in events)
            )

    def test_http_redacts_request_and_writes_only_explicit_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, _HttpServer() as server:
            root = Path(temp_dir)
            io = root / "io"
            io.mkdir()
            body = io / "request.bin"
            body.write_bytes(b"request-secret")
            artifact = io / "response.bin"
            origin = self._origin(server)
            lifecycle = self._lifecycle(root, origin)
            task = lifecycle.begin(self._contract(origin), idempotency_key="redact-begin")
            lifecycle.approve(
                task.task_id,
                stage="external_send",
                approved=True,
                approver="operator",
                rationale="approved test request",
                idempotency_key="redact-approval",
                proof="trusted",
            )
            action = {
                "kind": "http",
                "method": "POST",
                "url": origin + "/submit?token=query-secret",
                "headers": {"X-Public": "header-secret"},
                "body_ref": "io/request.bin",
                "timeout_seconds": 5,
                "expected_statuses": [201],
                "response_artifact": "io/response.bin",
                "auth_ref": "service-token",
            }

            completed = lifecycle.action(
                task.task_id, action, idempotency_key="redact-send"
            )
            self.assertEqual(artifact.read_bytes(), b"response-secret")
            body.unlink()
            artifact.unlink()
            io.rmdir()
            restarted = self._lifecycle(root, origin)
            replay = restarted.action(
                task.task_id, action, idempotency_key="redact-send"
            )

            self.assertEqual(completed, replay)
            self.assertEqual(server.calls, 1)  # type: ignore[attr-defined]
            self.assertEqual(
                server.received,  # type: ignore[attr-defined]
                {
                    "authorization": "Bearer credential-secret",
                    "public": "header-secret",
                    "body": b"request-secret",
                    "path": "/submit?token=query-secret",
                },
            )
            result = completed.idempotency[("action", "redact-send")].response
            self.assertEqual(result["status"], 201)
            self.assertTrue(result["expected"])
            self.assertTrue(result["artifact_written"])
            ledger_text = lifecycle.ledger.path.read_text(encoding="utf-8")
            for secret in (
                "query-secret",
                "header-secret",
                "request-secret",
                "response-secret",
                "credential-secret",
            ):
                self.assertNotIn(secret, ledger_text)

    def test_http_body_read_is_bounded_by_server_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            io = root / "io"
            io.mkdir()
            (io / "request.bin").write_bytes(b"placeholder")
            origin = "https://api.example"
            lifecycle = TaskLifecycle(
                root,
                policy=TaskPolicy(
                    allowed_tools=frozenset({"http"}),
                    allowed_network_origins=frozenset({origin}),
                    max_http_request_bytes=4,
                ),
            )
            stream = MagicMock()
            stream.__enter__.return_value = stream
            stream.read.return_value = b"12345"

            with patch.object(Path, "open", return_value=stream), self.assertRaises(
                TaskLifecycleError
            ) as caught:
                lifecycle._normalize_http_action(
                    self._contract(origin),
                    {
                        "kind": "http",
                        "method": "POST",
                        "url": origin,
                        "body_ref": "io/request.bin",
                        "expected_statuses": [200],
                    },
                )

            self.assertEqual(caught.exception.code, "validation_error")
            stream.read.assert_called_once_with(5)

    def test_http_rejects_sensitive_headers_and_scope_escape_before_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, _HttpServer() as server:
            root = Path(temp_dir)
            (root / "io").mkdir()
            origin = self._origin(server)
            lifecycle = self._lifecycle(root, origin)
            task = lifecycle.begin(self._contract(origin), idempotency_key="reject-begin")

            for key, action in (
                (
                    "raw-auth",
                    {
                        "kind": "http",
                        "method": "GET",
                        "url": origin,
                        "headers": {"Authorization": "raw-secret"},
                        "expected_statuses": [200],
                    },
                ),
                (
                    "raw-api-key",
                    {
                        "kind": "http",
                        "method": "GET",
                        "url": origin,
                        "headers": {"X-Api-Key": "raw-secret"},
                        "expected_statuses": [200],
                    },
                ),
                (
                    "host-override",
                    {
                        "kind": "http",
                        "method": "GET",
                        "url": origin,
                        "headers": {"Host": "outside.example"},
                        "expected_statuses": [200],
                    },
                ),
                (
                    "unknown-auth",
                    {
                        "kind": "http",
                        "method": "GET",
                        "url": origin,
                        "expected_statuses": [200],
                        "auth_ref": "unknown-token",
                    },
                ),
                (
                    "artifact-escape",
                    {
                        "kind": "http",
                        "method": "GET",
                        "url": origin,
                        "expected_statuses": [200],
                        "response_artifact": "outside.bin",
                    },
                ),
                (
                    "body-escape",
                    {
                        "kind": "http",
                        "method": "POST",
                        "url": origin,
                        "body_ref": "outside.bin",
                        "expected_statuses": [200],
                    },
                ),
                (
                    "origin-escape",
                    {
                        "kind": "http",
                        "method": "GET",
                        "url": "https://outside.example/path",
                        "expected_statuses": [200],
                    },
                ),
            ):
                with self.subTest(key=key), self.assertRaises(TaskLifecycleError):
                    lifecycle.action(task.task_id, action, idempotency_key=key)

            self.assertEqual(server.calls, 0)  # type: ignore[attr-defined]
            self.assertFalse(
                any(
                    event.event_type == AuditEventType.TASK_ACTION_INTENT.value
                    for event in lifecycle.ledger.events_for_contract(
                        task.task_id, all_segments=True
                    )
                )
            )

    def test_http_unexpected_status_is_completed_not_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, _HttpServer() as server:
            root = Path(temp_dir)
            (root / "io").mkdir()
            origin = self._origin(server)
            lifecycle = self._lifecycle(root, origin)
            task = lifecycle.begin(self._contract(origin), idempotency_key="status-begin")
            lifecycle.approve(
                task.task_id,
                stage="external_send",
                approved=True,
                approver="operator",
                rationale="approve status check",
                idempotency_key="status-approval",
                proof="trusted",
            )

            task = lifecycle.action(
                task.task_id,
                {
                    "kind": "http",
                    "method": "POST",
                    "url": origin + "/unexpected",
                    "expected_statuses": [200],
                },
                idempotency_key="status-send",
            )

            record = task.idempotency[("action", "status-send")]
            self.assertEqual(record.outcome, "completed")
            self.assertFalse(record.response["expected"])
            self.assertEqual(task.state, TaskState.EXECUTING)
            self.assertEqual(task.unresolved_intents, ())

    def test_http_transport_failure_blocks_and_restart_never_replays(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "io").mkdir()
            origin = "https://api.example"
            opener = _FailingOpener()
            policy = TaskPolicy(
                allowed_tools=frozenset({"http"}),
                allowed_network_origins=frozenset({origin}),
                allowed_http_headers=frozenset({"x-public"}),
            )
            lifecycle = TaskLifecycle(
                root,
                policy=policy,
                approval_authorizer=lambda _who, _stage, proof: proof == "trusted",
                http_adapter=HttpAdapter(opener=opener),
            )
            contract = GoalContract(
                "http failure",
                "do not replay",
                permissions=PermissionContract(
                    allowed_tools=("http",),
                    write_scope=("io",),
                    network_scope=(origin,),
                ),
            )
            task = lifecycle.begin(contract, idempotency_key="failure-begin")
            lifecycle.approve(
                task.task_id,
                stage="external_send",
                approved=True,
                approver="operator",
                rationale="approve failure probe",
                idempotency_key="failure-approval",
                proof="trusted",
            )
            action = {
                "kind": "http",
                "method": "POST",
                "url": origin + "/send?token=query-secret",
                "headers": {"X-Public": "header-secret"},
                "expected_statuses": [200],
            }

            with self.assertRaises(TaskLifecycleError) as caught:
                lifecycle.action(task.task_id, action, idempotency_key="failure-send")
            self.assertEqual(caught.exception.code, "action_failed")
            self.assertNotIn("query-secret", str(caught.exception))
            self.assertEqual(opener.calls, 1)

            restarted = TaskLifecycle(
                root,
                lifecycle.ledger.path,
                policy=policy,
                approval_authorizer=lambda _who, _stage, proof: proof == "trusted",
                http_adapter=HttpAdapter(opener=opener),
            )
            blocked = restarted.get(task.task_id)
            self.assertEqual(blocked.state, TaskState.BLOCKED)
            self.assertEqual(len(blocked.unresolved_intents), 1)
            with self.assertRaises(TaskLifecycleError) as replay:
                restarted.action(
                    task.task_id, action, idempotency_key="failure-send"
                )
            self.assertEqual(replay.exception.code, "unresolved_action_intent")
            self.assertEqual(opener.calls, 1)
            ledger_text = lifecycle.ledger.path.read_text(encoding="utf-8")
            for secret in ("query-secret", "header-secret", "transport-secret"):
                self.assertNotIn(secret, ledger_text)

    def test_task_policy_preserves_legacy_positional_field_order(self) -> None:
        policy = TaskPolicy(
            frozenset({"shell"}),
            (("python", "-c"),),
            (("python", "-m", "unittest"),),
            (("python", "-m"),),
            12.0,
        )

        self.assertEqual(policy.subprocess_argv_prefixes, (("python", "-c"),))
        self.assertEqual(
            policy.verification_commands,
            (("python", "-m", "unittest"),),
        )
        self.assertEqual(policy.verification_argv_prefixes, (("python", "-m"),))
        self.assertEqual(policy.max_timeout_seconds, 12.0)
        self.assertEqual(policy.allowed_network_origins, frozenset())
        with self.assertRaisesRegex(ValueError, "credential headers"):
            TaskPolicy(allowed_http_headers=frozenset({"Authorization"}))

    def test_restart_enforces_current_network_ceiling_before_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "io").mkdir()
            original = "https://a.example"
            lifecycle = TaskLifecycle(
                root,
                policy=TaskPolicy(
                    allowed_tools=frozenset({"http"}),
                    allowed_network_origins=frozenset({original}),
                ),
                approval_authorizer=lambda _who, _stage, proof: proof == "trusted",
            )
            task = lifecycle.begin(
                GoalContract(
                    "policy restart",
                    "do not retain widened server policy",
                    permissions=PermissionContract(
                        allowed_tools=("http",),
                        write_scope=("io",),
                        network_scope=(original,),
                    ),
                ),
                idempotency_key="policy-restart-begin",
            )
            lifecycle.approve(
                task.task_id,
                stage="external_send",
                approved=True,
                approver="operator",
                rationale="approval under original policy",
                idempotency_key="policy-restart-approval",
                proof="trusted",
            )
            opener = _FailingOpener()
            restarted = TaskLifecycle(
                root,
                lifecycle.ledger.path,
                policy=TaskPolicy(
                    allowed_tools=frozenset({"http"}),
                    allowed_network_origins=frozenset({"https://b.example"}),
                ),
                approval_authorizer=lambda _who, _stage, proof: proof == "trusted",
                http_adapter=HttpAdapter(opener=opener),
            )

            with self.assertRaises(TaskLifecycleError) as caught:
                restarted.action(
                    task.task_id,
                    {
                        "kind": "http",
                        "method": "GET",
                        "url": original,
                        "expected_statuses": [200],
                    },
                    idempotency_key="policy-restart-send",
                )

            self.assertEqual(caught.exception.code, "policy_denied")
            self.assertEqual(opener.calls, 0)
            self.assertFalse(
                any(
                    event.event_type == AuditEventType.TASK_ACTION_INTENT.value
                    and event.payload.get("idempotency_key") == "policy-restart-send"
                    for event in restarted.ledger.events_for_contract(
                        task.task_id, all_segments=True
                    )
                )
            )


if __name__ == "__main__":
    unittest.main()
