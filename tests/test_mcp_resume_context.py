from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import (
    AuditEventType,
    GoalContract,
    PermissionContract,
    StateTransition,
    VerificationRequirement,
)
from causality.mcp_server import CausalityMCPServer
from causality.memory import TypedMemory
from causality.task_lifecycle import TaskLifecycle, TaskPolicy


VERIFY_COMMAND = (sys.executable, "-c", "print('resume-pass')")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    return value


class SimulatedProcessDeath(BaseException):
    """Leave an action intent durable while no result is recorded."""


class MCPResumeContextTests(unittest.TestCase):
    @staticmethod
    def _server(
        root: str | Path,
        *,
        approval_token: str | None = None,
    ) -> CausalityMCPServer:
        return CausalityMCPServer(
            root,
            approval_token=approval_token,
            policy=TaskPolicy(verification_commands=(VERIFY_COMMAND,)),
        )

    @staticmethod
    def _contract(title: str = "resume task") -> GoalContract:
        return GoalContract(
            title=title,
            summary="exercise the read-only durable resume projection",
            permissions=PermissionContract(
                allowed_tools=("file.read", "file.write", "shell"),
                write_scope=("out",),
            ),
            verification_requirements=(
                VerificationRequirement("resume-pass", VERIFY_COMMAND),
            ),
            non_goals=("write outside the project",),
            stopping_policy={
                "max_iterations": 8,
                "max_failed_hypotheses": 3,
                "no_progress_iterations": 2,
            },
        )

    def _call(
        self,
        server: CausalityMCPServer,
        name: str,
        arguments: dict[str, Any],
        *,
        request_id: int = 1,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        self.assertIsNotNone(response)
        assert response is not None
        self.assertNotIn("error", response)
        result = response["result"]
        payload = json.loads(result["content"][0]["text"])
        return result, payload

    def _resume(
        self,
        server: CausalityMCPServer,
        task_id: str,
        *,
        request_id: int = 1,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._call(
            server,
            "causality_task_resume",
            {"task_id": task_id},
            request_id=request_id,
        )

    def test_resume_schema_is_closed_and_has_no_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            response = self._server(temp_dir).handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
            assert response is not None
            tools = {tool["name"]: tool for tool in response["result"]["tools"]}

            schema = tools["causality_task_resume"]["inputSchema"]
            self.assertEqual(schema["required"], ["task_id"])
            self.assertEqual(set(schema["properties"]), {"task_id"})
            self.assertIs(schema["additionalProperties"], False)
            self.assertNotIn("idempotency_key", schema["properties"])

    def test_resume_rebuilds_mid_phase_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = self._server(root)
            task = first.lifecycle.begin(
                self._contract(),
                idempotency_key="begin-mid-phase",
                workflow="root-cause-protocol",
            )
            phase_id = task.workflow_phases[0].phase_id
            started = first.lifecycle.phase(
                task.task_id,
                phase_id=phase_id,
                action="start",
                idempotency_key="start-mid-phase",
            )
            before_bytes = first.ledger.path.read_bytes()
            before_count = first.ledger.event_count()

            result, payload = self._resume(self._server(root), task.task_id)

            self.assertFalse(result.get("isError", False))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["task"], started.to_dict())
            self.assertEqual(payload["data"]["contract"], _plain(started.contract_snapshot))
            self.assertEqual(payload["data"]["unmet_verification"], ["resume-pass"])
            self.assertIsNone(payload["data"]["terminal_result"])
            self.assertIsNone(payload["data"]["reflection_result"])
            self.assertEqual(payload["task"]["current_phase_id"], phase_id)
            self.assertEqual(payload["task"]["workflow_phases"][0]["status"], "running")
            self.assertEqual(payload["task"]["workflow_phases"][0]["attempt"], 1)
            self.assertNotIn("idempotency", payload)
            self.assertNotIn("event_hash", payload)
            self.assertEqual(first.ledger.event_count(), before_count)
            self.assertEqual(first.ledger.path.read_bytes(), before_bytes)

            _, replay = self._resume(self._server(root), task.task_id, request_id=2)
            self.assertEqual(replay, payload)
            self.assertEqual(first.ledger.event_count(), before_count)
            self.assertEqual(first.ledger.path.read_bytes(), before_bytes)

    def test_resume_marks_verification_unmet_after_mutating_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = self._server(root)
            task = server.lifecycle.begin(
                self._contract(), idempotency_key="begin-freshness", workflow="legacy"
            )
            verified = server.lifecycle.verify(
                task.task_id,
                "resume-pass",
                idempotency_key="verify-before-mutation",
            )
            _, fresh = self._resume(server, task.task_id)
            self.assertEqual(fresh["data"]["unmet_verification"], [])
            self.assertEqual(
                verified.requirement_results["resume-pass"]["status"],
                "pass",
            )

            server.lifecycle.action(
                task.task_id,
                {"kind": "file_write", "path": "out/changed.txt", "content": "changed\n"},
                idempotency_key="mutate-after-verification",
            )
            count = server.ledger.event_count()

            _, stale = self._resume(self._server(root), task.task_id)

            self.assertEqual(stale["data"]["unmet_verification"], ["resume-pass"])
            self.assertEqual(
                stale["task"]["requirement_results"]["resume-pass"]["status"],
                "pass",
                "resume must distinguish a recorded PASS from a currently fresh PASS",
            )
            self.assertEqual(server.ledger.event_count(), count)

    def test_resume_orphan_action_exposes_only_safe_recovery_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = self._server(root)
            secret_content = "never-expose-or-replay-this-content"
            effects: list[dict[str, Any]] = []

            def die(action: dict[str, Any]) -> dict[str, Any]:
                effects.append(action)
                raise SimulatedProcessDeath

            server.lifecycle = TaskLifecycle(
                root,
                server.ledger.path,
                policy=server.lifecycle.policy,
                effect_runner=die,
            )
            task = server.lifecycle.begin(
                self._contract(), idempotency_key="begin-orphan", workflow="legacy"
            )
            with self.assertRaises(SimulatedProcessDeath):
                server.lifecycle.action(
                    task.task_id,
                    {
                        "kind": "file_write",
                        "path": "out/orphan.txt",
                        "content": secret_content,
                    },
                    idempotency_key="orphan-effect",
                )
            count = server.ledger.event_count()

            _, payload = self._resume(self._server(root), task.task_id)
            serialized = json.dumps(payload, sort_keys=True)

            self.assertEqual(payload["task"]["state"], "blocked")
            self.assertEqual(payload["task"]["allowed_next"], ["resolve"])
            self.assertEqual(len(payload["data"]["pending_intents"]), 1)
            pending = payload["data"]["pending_intents"][0]
            self.assertEqual(
                set(pending), {"kind", "operation", "operation_id", "event_hash"}
            )
            self.assertEqual(pending["kind"], "action")
            self.assertEqual(pending["operation"], "action")
            self.assertNotIn(secret_content, serialized)
            self.assertNotIn("descriptor", serialized)
            self.assertNotIn("idempotency_key", serialized)
            self.assertEqual(len(effects), 1)
            self.assertEqual(server.ledger.event_count(), count)
            _, context = self._call(
                self._server(root), "causality_context", {"limit": 10}, request_id=2
            )
            context_text = json.dumps(context, sort_keys=True)
            self.assertNotIn(secret_content, context_text)
            self.assertNotIn("orphan-effect", context_text)
            self.assertEqual(
                set(context["ledger_tail"][0]),
                {
                    "event_id",
                    "event_type",
                    "timestamp",
                    "contract_ref",
                    "entry_hash",
                },
            )

    def test_terminal_and_reflected_results_are_replayed_without_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = self._server(root)
            task = server.lifecycle.begin(
                self._contract(), idempotency_key="begin-terminal", workflow="legacy"
            )
            verification = server.lifecycle.verify(
                task.task_id, "resume-pass", idempotency_key="terminal-verify"
            )
            evidence = verification.requirement_results["resume-pass"][
                "evidence_event_hash"
            ]
            for number in (1, 2):
                server.lifecycle.verdict(
                    task.task_id,
                    verifier=f"verifier-{number}",
                    status="pass",
                    rationale="independent evidence review passed",
                    evidence_refs=(evidence,),
                    idempotency_key=f"terminal-verdict-{number}",
                )
            completed = server.lifecycle.complete(
                task.task_id, idempotency_key="terminal-complete"
            )
            self.assertEqual(completed.state.value, "verified")
            reflected = server.lifecycle.reflect(
                task.task_id,
                idempotency_key="terminal-reflect",
                failure_scope=f"task:{task.task_id}",
                failure_ttl_days=30,
            )
            count = server.ledger.event_count()
            memory_bytes = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.glob("memory/**/*.jsonl")
            }

            _, payload = self._resume(self._server(root), task.task_id)

            self.assertEqual(payload["task"], reflected.to_dict())
            self.assertEqual(
                payload["data"]["unmet_verification"],
                [],
                "reflection runtime JSONL must not stale the verified workspace",
            )
            self.assertEqual(payload["data"]["terminal_result"]["operation"], "complete")
            self.assertEqual(payload["data"]["terminal_result"]["data"]["decision"], "pass")
            self.assertEqual(
                payload["data"]["reflection_result"]["data"],
                _plain(reflected.reflection["response"]),
            )
            self.assertEqual(server.ledger.event_count(), count)
            self.assertEqual(
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.glob("memory/**/*.jsonl")
                },
                memory_bytes,
            )
            _, replay = self._resume(self._server(root), task.task_id, request_id=2)
            self.assertEqual(replay, payload)
            self.assertEqual(server.ledger.event_count(), count)

    def test_curated_markdown_stales_verification_but_runtime_jsonl_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = self._server(root)
            task = server.lifecycle.begin(
                self._contract(), idempotency_key="begin-knowledge", workflow="legacy"
            )
            server.lifecycle.verify(
                task.task_id,
                "resume-pass",
                idempotency_key="verify-knowledge",
            )
            runtime = root / "skills" / "candidates" / "log.jsonl"
            runtime.parent.mkdir(parents=True)
            runtime.write_text('{"local":"runtime"}\n', encoding="utf-8")

            _, runtime_only = self._resume(server, task.task_id)
            self.assertEqual(runtime_only["data"]["unmet_verification"], [])

            curated = root / "skills" / "curated.jsonl.md"
            curated.write_text("# Shared skill\n", encoding="utf-8")
            _, curated_change = self._resume(server, task.task_id)
            self.assertEqual(
                curated_change["data"]["unmet_verification"], ["resume-pass"]
            )

    def test_rejected_result_and_resume_errors_fail_closed_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = self._server(root, approval_token="trusted")
            task = server.lifecycle.begin(
                self._contract("rejected task"),
                idempotency_key="begin-rejected",
                workflow="legacy",
            )
            rejected = server.lifecycle.approve(
                task.task_id,
                stage="plan",
                approved=False,
                approver="operator",
                rationale="reject this task",
                evidence_refs=(),
                idempotency_key="reject-plan",
                proof="trusted",
            )
            count = server.ledger.event_count()

            _, payload = self._resume(self._server(root), task.task_id)
            self.assertEqual(payload["task"], rejected.to_dict())
            self.assertEqual(payload["data"]["terminal_result"]["operation"], "approve")
            self.assertIs(payload["data"]["terminal_result"]["data"]["approved"], False)
            self.assertEqual(server.ledger.event_count(), count)

            for arguments, code in (
                ({}, "validation_error"),
                ({"task_id": ""}, "invalid_task_id"),
                ({"task_id": "missing-task"}, "task_not_found"),
                ({"task_id": task.task_id, "extra": True}, "validation_error"),
            ):
                with self.subTest(arguments=arguments):
                    result, failed = self._call(
                        server, "causality_task_resume", arguments, request_id=3
                    )
                    self.assertTrue(result["isError"])
                    self.assertFalse(failed["ok"])
                    self.assertEqual(failed["error"]["code"], code)
                    self.assertEqual(server.ledger.event_count(), count)

            lines = server.ledger.path.read_text(encoding="utf-8").splitlines()
            first = json.loads(lines[0])
            first["payload"]["title"] = "tampered task"
            lines[0] = json.dumps(
                first, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
            server.ledger.path.write_text(
                "\n".join(lines) + "\n",
                encoding="utf-8",
                newline="",
            )
            tampered_size = server.ledger.path.stat().st_size
            result, failed = self._resume(server, task.task_id, request_id=4)
            self.assertTrue(result["isError"])
            self.assertEqual(failed["error"]["code"], "ledger_integrity_failed")
            self.assertEqual(server.ledger.path.stat().st_size, tampered_size)
            result, failed = self._call(
                server, "causality_context", {"limit": 5}, request_id=5
            )
            self.assertTrue(result["isError"])
            self.assertEqual(failed["error"]["code"], "ledger_integrity_failed")

    def test_terminal_projection_requires_cause_operation_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = self._server(root)
            task = server.lifecycle.begin(
                self._contract("forged terminal"),
                idempotency_key="begin-forged-terminal",
                workflow="legacy",
            )
            started = server.ledger.events_for_contract(
                task.task_id, all_segments=True
            )[-1]
            approved = server.ledger.append(
                AuditEventType.STATE_TRANSITION,
                {
                    "schema_version": 1,
                    "task_id": task.task_id,
                    "from_state": StateTransition.PLANNED.value,
                    "state": StateTransition.APPROVED.value,
                    "reason": "forged setup",
                    "cause_event_hash": started.entry_hash,
                },
                contract_id=task.task_id,
            )
            executing = server.ledger.append(
                AuditEventType.STATE_TRANSITION,
                {
                    "schema_version": 1,
                    "task_id": task.task_id,
                    "from_state": StateTransition.APPROVED.value,
                    "state": StateTransition.EXECUTING.value,
                    "reason": "forged setup",
                    "cause_event_hash": approved.entry_hash,
                },
                contract_id=task.task_id,
            )
            server.ledger.append(
                AuditEventType.STATE_TRANSITION,
                {
                    "schema_version": 1,
                    "task_id": task.task_id,
                    "from_state": StateTransition.EXECUTING.value,
                    "state": StateTransition.VERIFIED.value,
                    "reason": "forged terminal without completion",
                    "cause_event_hash": executing.entry_hash,
                },
                contract_id=task.task_id,
            )

            result, payload = self._resume(server, task.task_id)

            self.assertTrue(result["isError"])
            self.assertEqual(payload["error"]["code"], "invalid_task_event")

    def test_context_filters_expired_failures_and_separates_curated_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            memory = TypedMemory(root)
            active = memory.record_failure(
                "active failure guardrail",
                scope="project",
                ttl_days=30,
            )
            memory.record_once(
                "failures",
                "expired failure guardrail",
                entry_id="expired-failure",
                created_at="2000-01-01T00:00:00+00:00",
                scope="project",
                ttl_days=1,
            )
            (root / "memory" / "decisions").mkdir(parents=True, exist_ok=True)
            (root / "memory" / "decisions" / "README.md").write_text(
                "# Curated decision\n", encoding="utf-8"
            )
            (root / "skills").mkdir(parents=True, exist_ok=True)
            (root / "skills" / "curated-skill.md").write_text(
                "# Curated skill\n", encoding="utf-8"
            )
            (root / "skills" / "runtime.jsonl").write_text(
                '{"secret":"local-only"}\n', encoding="utf-8"
            )

            result, payload = self._call(
                self._server(root), "causality_context", {"limit": 5}
            )

            self.assertFalse(result.get("isError", False))
            self.assertTrue(payload["ok"])
            active_failures = payload["knowledge"]["active_failures"]
            self.assertEqual([item["entry_id"] for item in active_failures], [active.entry_id])
            self.assertEqual(
                payload["knowledge"]["curated_markdown"],
                {
                    "memory": ["memory/decisions/README.md"],
                    "skills": ["skills/curated-skill.md"],
                },
            )
            self.assertEqual(
                payload["knowledge"]["runtime_jsonl"],
                {
                    "classification": "local_runtime",
                    "recommended_ignore_patterns": [
                        "memory/**/*.jsonl",
                        "skills/**/*.jsonl",
                    ],
                },
            )
            serialized = json.dumps(payload, sort_keys=True)
            self.assertNotIn("expired failure guardrail", serialized)
            self.assertNotIn("runtime.jsonl", serialized)
            self.assertNotIn("local-only", serialized)


if __name__ == "__main__":
    unittest.main()
