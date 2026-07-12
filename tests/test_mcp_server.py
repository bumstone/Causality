from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.mcp_server import CausalityMCPServer
from causality.task_lifecycle import TaskLifecycle


LIFECYCLE_TOOLS = {
    "causality_task_begin",
    "causality_task_approve",
    "causality_task_action",
    "causality_task_verify",
    "causality_task_verdict",
    "causality_task_complete",
    "causality_task_resolve",
    "causality_task_reflect",
}

WIRE_COMMAND = (sys.executable, "-m", "unittest")


class SimulatedProcessDeath(BaseException):
    """Leave a durable action intent without an action result."""


class MCPServerTests(unittest.TestCase):
    def test_server_blocks_pretracked_private_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            ledger = root / ".causality" / "ledger.jsonl"
            ledger.parent.mkdir()
            ledger.write_text("tracked legacy state\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "-f", ".causality/ledger.jsonl"], cwd=root, check=True
            )

            with self.assertRaisesRegex(ValueError, "already tracked by Git"):
                CausalityMCPServer(root)

            self.assertEqual(ledger.read_text(encoding="utf-8"), "tracked legacy state\n")

    def test_server_rejects_symlinked_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside:
            root = Path(temp_dir)
            try:
                (root / ".causality").symlink_to(Path(outside), target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")

            with self.assertRaisesRegex(ValueError, "contains a symlink"):
                CausalityMCPServer(root)

            self.assertFalse((Path(outside) / "ledger.jsonl").exists())

    @staticmethod
    def _server(project: str | Path) -> CausalityMCPServer:
        return CausalityMCPServer(project)

    @staticmethod
    def _begin_arguments(
        *,
        key: str = "begin-wire",
        objective: str = "exercise the durable MCP lifecycle",
        risk: str = "low",
    ) -> dict[str, Any]:
        return {
            "objective": objective,
            "risk": risk,
            "permissions": {
                "allowed_tools": ["file.read", "file.write", "shell"],
                "write_scope": ["out"],
                "network_scope": [],
                "auth_scope": [],
            },
            "verification_requirements": [
                {
                    "id": "wire-pass",
                    "argv": list(WIRE_COMMAND),
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
            "non_goals": ["write outside the project"],
            "idempotency_key": key,
        }

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
        self.assertNotIn(
            "error",
            response,
            "tool-domain failures belong in an MCP isError result, not JSON-RPC error",
        )
        result = response["result"]
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["type"], "text")
        payload = json.loads(result["content"][0]["text"])
        self.assertIsInstance(payload, dict)
        return result, payload

    def _assert_success(
        self,
        result: dict[str, Any],
        payload: dict[str, Any],
        *,
        key: str,
        replayed: bool = False,
    ) -> None:
        self.assertFalse(result.get("isError", False))
        self.assertIs(payload.get("ok"), True)
        self.assertIn("task", payload)
        self.assertRegex(payload.get("event_hash", ""), r"^[0-9a-f]{64}$")
        self.assertIsInstance(payload.get("data"), dict)
        self.assertEqual(
            payload.get("idempotency"), {"key": key, "replayed": replayed}
        )

    def _assert_domain_error(
        self,
        result: dict[str, Any],
        payload: dict[str, Any],
        *,
        code: str | None = None,
    ) -> None:
        self.assertIs(result.get("isError"), True)
        self.assertIs(payload.get("ok"), False)
        error = payload["error"]
        self.assertIsInstance(error["code"], str)
        self.assertIsInstance(error["message"], str)
        self.assertIsInstance(error["retryable"], bool)
        self.assertIn("details", error)
        if code is not None:
            self.assertEqual(error["code"], code)

    def _begin(
        self,
        server: CausalityMCPServer,
        *,
        key: str = "begin-wire",
        risk: str = "low",
    ) -> tuple[str, dict[str, Any]]:
        result, payload = self._call(
            server,
            "causality_task_begin",
            self._begin_arguments(key=key, risk=risk),
        )
        self._assert_success(result, payload, key=key)
        task = payload["task"]
        self.assertEqual(task["task_id"], task["contract_id"])
        return task["task_id"], payload

    def test_tools_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self._server(temp_dir)
            response = server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )

            assert response is not None
            tools = {tool["name"]: tool for tool in response["result"]["tools"]}
            self.assertTrue(
                LIFECYCLE_TOOLS.issubset(tools),
                f"missing lifecycle tools: {sorted(LIFECYCLE_TOOLS - tools.keys())}",
            )
            self.assertIn("causality_context", tools)
            self.assertIn("causality_append_evidence", tools)
            self.assertIn("deprecated", tools["causality_append_evidence"]["description"].lower())

            init = tools["causality_init"]
            self.assertIn("client", init["inputSchema"]["properties"])
            self.assertIn("verify", init["inputSchema"]["properties"])
            self.assertNotIn("force", init["inputSchema"]["properties"])
            self.assertNotIn("adopt", init["inputSchema"]["properties"])

            for name, tool in tools.items():
                with self.subTest(tool=name):
                    schema = tool["inputSchema"]
                    self.assertEqual(schema["type"], "object")
                    self.assertIs(
                        schema.get("additionalProperties"),
                        False,
                        f"{name} must reject misspelled/unknown inputs",
                    )

    def test_lifecycle_tool_schemas_are_closed_and_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            response = self._server(temp_dir).handle(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            )
            assert response is not None
            tools = {tool["name"]: tool for tool in response["result"]["tools"]}

            begin = tools["causality_task_begin"]["inputSchema"]
            self.assertTrue(
                {
                    "objective",
                    "risk",
                    "permissions",
                    "verification_requirements",
                    "stop_condition",
                    "idempotency_key",
                }.issubset(begin["required"])
            )
            self.assertNotIn("task_id", begin["properties"])
            self.assertEqual(
                begin["properties"]["risk"]["enum"],
                ["low", "medium", "high", "irreversible"],
            )
            evidence_item = begin["properties"]["evidence_required"]["items"]
            self.assertEqual(
                set(evidence_item["properties"]["kind"]["enum"]),
                {
                    "test_output",
                    "browser_diff",
                    "artifact_hash",
                    "tool_output",
                    "a11y_report",
                    "verification_result",
                },
            )

            for name in LIFECYCLE_TOOLS - {"causality_task_begin"}:
                with self.subTest(tool=name):
                    schema = tools[name]["inputSchema"]
                    self.assertTrue(
                        {"task_id", "idempotency_key"}.issubset(schema["required"])
                    )
                    self.assertNotIn("contract_id", schema["properties"])
                    for field in ("task_id", "idempotency_key"):
                        self.assertEqual(schema["properties"][field]["type"], "string")
                        self.assertGreaterEqual(
                            schema["properties"][field].get("minLength", 0), 1
                        )

            action = tools["causality_task_action"]["inputSchema"]["properties"][
                "action"
            ]
            branches = action["oneOf"]
            self.assertEqual(
                {branch["properties"]["kind"]["const"] for branch in branches},
                {"file_read", "file_write", "subprocess"},
            )
            for branch in branches:
                self.assertIs(branch.get("additionalProperties"), False)
                self.assertNotIn("tool", branch["properties"])
            process = next(
                branch
                for branch in branches
                if branch["properties"]["kind"]["const"] == "subprocess"
            )
            self.assertEqual(process["properties"]["argv"]["type"], "array")
            self.assertEqual(process["properties"]["argv"]["items"]["type"], "string")

            verify = tools["causality_task_verify"]["inputSchema"]
            self.assertTrue({"requirement_id", "mode"}.issubset(verify["required"]))
            self.assertEqual(verify["properties"]["mode"]["enum"], ["execute", "manual"])
            self.assertTrue(
                {"approved", "approver", "rationale", "evidence_hash"}.issubset(
                    verify["properties"]
                )
            )

            approve = tools["causality_task_approve"]["inputSchema"]
            self.assertTrue(
                {
                    "stage",
                    "approved",
                    "approver",
                    "rationale",
                    "evidence_refs",
                    "proof",
                }.issubset(approve["required"])
            )
            verdict = tools["causality_task_verdict"]["inputSchema"]
            self.assertTrue(
                {"verifier", "status", "rationale", "evidence_refs"}.issubset(
                    verdict["required"]
                )
            )
            self.assertEqual(verdict["properties"]["status"]["enum"], ["pass", "fail"])
            resolve = tools["causality_task_resolve"]["inputSchema"]
            self.assertTrue(
                {"operation_id", "resolution", "proof"}.issubset(resolve["required"])
            )
            self.assertEqual(
                resolve["properties"]["resolution"]["enum"],
                ["applied", "not_applied", "reject"],
            )
            reflect = tools["causality_task_reflect"]["inputSchema"]
            self.assertIn("scope", reflect["properties"])
            self.assertIn("ttl_days", reflect["properties"])

            evidence = tools["causality_append_evidence"]["inputSchema"]
            self.assertTrue(
                {"task_id", "idempotency_key", "kind", "payload"}.issubset(
                    evidence["required"]
                )
            )
            self.assertNotIn("contract_id", evidence["properties"])

    def test_begin_retry_replays_and_conflicting_request_is_a_domain_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self._server(temp_dir)
            arguments = self._begin_arguments(key="one-begin")

            first_result, first = self._call(
                server, "causality_task_begin", arguments, request_id=10
            )
            self._assert_success(first_result, first, key="one-begin")
            count = server.ledger.event_count()

            replay_result, replay = self._call(
                self._server(temp_dir),
                "causality_task_begin",
                copy.deepcopy(arguments),
                request_id=11,
            )
            self._assert_success(
                replay_result, replay, key="one-begin", replayed=True
            )
            self.assertEqual(replay["task"], first["task"])
            self.assertEqual(replay["event_hash"], first["event_hash"])
            self.assertEqual(server.ledger.event_count(), count)

            conflicting = copy.deepcopy(arguments)
            conflicting["objective"] = "a different task cannot reuse the key"
            error_result, error = self._call(
                self._server(temp_dir),
                "causality_task_begin",
                conflicting,
                request_id=12,
            )
            self._assert_domain_error(
                error_result, error, code="idempotency_conflict"
            )
            self.assertEqual(server.ledger.event_count(), count)

    def test_begin_rejects_malformed_evidence_without_persisting_task(self) -> None:
        malformed = (
            {"kind": "", "description": "output", "required": True},
            {"kind": "not_evidence", "description": "output", "required": True},
            {"kind": "human_approval", "description": "output", "required": True},
            {"kind": "verifier_pass", "description": "output", "required": True},
            {"kind": 7, "description": "output", "required": True},
            {"kind": "tool_output", "description": " ", "required": True},
            {"kind": "tool_output", "description": 7, "required": True},
            {"kind": "tool_output", "description": "output", "required": 1},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self._server(temp_dir)
            for index, evidence in enumerate(malformed):
                with self.subTest(evidence=evidence):
                    arguments = self._begin_arguments(key=f"invalid-evidence-{index}")
                    arguments["evidence_required"] = [evidence]
                    result, payload = self._call(
                        server,
                        "causality_task_begin",
                        arguments,
                        request_id=13 + index,
                    )
                    self._assert_domain_error(
                        result,
                        payload,
                        code="validation_error",
                    )
                    self.assertEqual(
                        server.ledger.event_count(),
                        0,
                        "invalid input must fail before a task contract is durable",
                    )

    def test_unknown_fields_and_shell_strings_are_domain_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self._server(temp_dir)
            unknown = self._begin_arguments(key="unknown-field")
            unknown["contract_id"] = "caller-must-not-select-it"
            result, payload = self._call(
                server, "causality_task_begin", unknown, request_id=20
            )
            self._assert_domain_error(result, payload, code="validation_error")

            task_id, _ = self._begin(server, key="shell-string-task")
            action_result, action_payload = self._call(
                server,
                "causality_task_action",
                {
                    "task_id": task_id,
                    "idempotency_key": "shell-string",
                    "action": {
                        "kind": "subprocess",
                        "argv": "python -c print('must not use a shell')",
                        "cwd": ".",
                    },
                },
                request_id=21,
            )
            self._assert_domain_error(
                action_result, action_payload, code="validation_error"
            )

            classified_result, classified_payload = self._call(
                server,
                "causality_task_action",
                {
                    "task_id": task_id,
                    "idempotency_key": "caller-classification",
                    "action": {
                        "kind": "file_write",
                        "path": "out/forbidden.txt",
                        "content": "not written",
                        "tool": "file.read",
                    },
                },
                request_id=22,
            )
            self._assert_domain_error(
                classified_result, classified_payload, code="validation_error"
            )
            self.assertFalse((Path(temp_dir) / "out" / "forbidden.txt").exists())

    def test_arbitrary_verification_and_untrusted_high_risk_approval_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self._server(temp_dir)
            arbitrary = self._begin_arguments(key="arbitrary-command")
            arbitrary["verification_requirements"][0]["argv"] = [
                sys.executable,
                "-c",
                "print('caller-selected code')",
            ]
            result, payload = self._call(
                server,
                "causality_task_begin",
                arbitrary,
                request_id=23,
            )
            self._assert_domain_error(result, payload, code="policy_denied")

            task_id, _ = self._begin(server, key="high-risk", risk="high")
            decision = {
                "task_id": task_id,
                "idempotency_key": "untrusted-plan",
                "stage": "plan",
                "approved": True,
                "approver": "operator",
                "rationale": "reviewed",
                "evidence_refs": [],
                "proof": "not-configured",
            }
            denied_result, denied = self._call(
                server,
                "causality_task_approve",
                decision,
                request_id=24,
            )
            self._assert_domain_error(
                denied_result,
                denied,
                code="approval_required",
            )
            decision["idempotency_key"] = "trusted-plan"
            decision["proof"] = "trusted-secret"
            trusted_result, trusted = self._call(
                CausalityMCPServer(temp_dir, approval_token="trusted-secret"),
                "causality_task_approve",
                decision,
                request_id=25,
            )
            self._assert_success(trusted_result, trusted, key="trusted-plan")
            self.assertEqual(trusted["task"]["state"], "approved")

    def test_foreign_evidence_and_one_verifier_cannot_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self._server(temp_dir)
            first_id, _ = self._begin(server, key="citation-first")
            second_id, _ = self._begin(server, key="citation-second")

            def verify(task_id: str, key: str, request_id: int) -> str:
                result, payload = self._call(
                    server,
                    "causality_task_verify",
                    {
                        "task_id": task_id,
                        "idempotency_key": key,
                        "requirement_id": "wire-pass",
                        "mode": "execute",
                    },
                    request_id=request_id,
                )
                self._assert_success(result, payload, key=key)
                return payload["event_hash"]

            first_hash = verify(first_id, "citation-first-verify", 26)
            second_hash = verify(second_id, "citation-second-verify", 27)
            foreign_result, foreign = self._call(
                server,
                "causality_task_verdict",
                {
                    "task_id": first_id,
                    "idempotency_key": "foreign-verdict",
                    "verifier": "security",
                    "status": "pass",
                    "rationale": "wrong task evidence",
                    "severity": "normal",
                    "evidence_refs": [second_hash],
                },
                request_id=28,
            )
            self._assert_domain_error(
                foreign_result,
                foreign,
                code="evidence_scope_mismatch",
            )
            verdict_result, verdict = self._call(
                server,
                "causality_task_verdict",
                {
                    "task_id": first_id,
                    "idempotency_key": "one-verdict",
                    "verifier": "security",
                    "status": "pass",
                    "rationale": "one independent check",
                    "severity": "normal",
                    "evidence_refs": [first_hash],
                },
                request_id=29,
            )
            self._assert_success(verdict_result, verdict, key="one-verdict")
            complete_result, incomplete = self._call(
                server,
                "causality_task_complete",
                {"task_id": first_id, "idempotency_key": "one-complete"},
                request_id=30,
            )
            self._assert_success(complete_result, incomplete, key="one-complete")
            self.assertEqual(incomplete["data"]["decision"], "repair")
            self.assertEqual(incomplete["task"]["state"], "executing")

    def test_resolve_wire_recovers_orphan_without_replaying_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = self._server(root)
            effect_calls: list[dict[str, Any]] = []
            attempts = root / "out" / "effect-attempts.txt"

            def die_after_effect(action: dict[str, Any]) -> dict[str, Any]:
                effect_calls.append(action)
                attempts.parent.mkdir(parents=True, exist_ok=True)
                with attempts.open("a", encoding="utf-8") as handle:
                    handle.write("attempted\n")
                raise SimulatedProcessDeath("process died after the effect began")

            first.lifecycle = TaskLifecycle(
                root,
                first.ledger.path,
                policy=first.lifecycle.policy,
                approval_authorizer=lambda _principal, _stage, _proof: False,
                effect_runner=die_after_effect,
            )
            task_id, _ = self._begin(first, key="resolve-begin")
            target = root / "out" / "recovered.txt"
            with self.assertRaises(SimulatedProcessDeath):
                self._call(
                    first,
                    "causality_task_action",
                    {
                        "task_id": task_id,
                        "idempotency_key": "orphan-action",
                        "action": {
                            "kind": "file_write",
                            "path": "out/recovered.txt",
                            "content": "recovered\n",
                        },
                    },
                    request_id=30,
                )
            self.assertEqual(len(effect_calls), 1)
            self.assertEqual(attempts.read_text(encoding="utf-8"), "attempted\n")
            self.assertFalse(target.exists())

            restarted = CausalityMCPServer(
                root,
                approval_token="trusted-secret",
            )
            blocked = restarted.lifecycle.get(task_id)
            self.assertEqual(blocked.state.value, "blocked")
            operation_id = blocked.pending_operation_id
            self.assertIsInstance(operation_id, str)
            assert operation_id is not None
            resolution = {
                "task_id": task_id,
                "idempotency_key": "resolve-orphan",
                "operation_id": operation_id,
                "resolution": "not_applied",
                "approver": "operator",
                "rationale": "the intended target was not written",
                "proof": "wrong-secret",
            }
            denied_result, denied = self._call(
                restarted,
                "causality_task_resolve",
                resolution,
                request_id=31,
            )
            self._assert_domain_error(
                denied_result,
                denied,
                code="approval_required",
            )
            self.assertEqual(denied["task"]["state"], "blocked")
            self.assertEqual(
                denied["task"]["pending_operation_id"],
                operation_id,
            )
            self.assertEqual(len(effect_calls), 1)
            self.assertEqual(attempts.read_text(encoding="utf-8"), "attempted\n")

            resolution["proof"] = "trusted-secret"
            resolved_result, resolved = self._call(
                restarted,
                "causality_task_resolve",
                resolution,
                request_id=32,
            )
            self._assert_success(
                resolved_result,
                resolved,
                key="resolve-orphan",
            )
            self.assertEqual(resolved["task"]["state"], "executing")
            self.assertEqual(resolved["data"]["resolution"], "not_applied")
            self.assertIsNone(resolved["task"]["pending_operation_id"])
            self.assertEqual(len(effect_calls), 1)
            self.assertEqual(attempts.read_text(encoding="utf-8"), "attempted\n")
            self.assertFalse(target.exists())

            action_result, action = self._call(
                restarted,
                "causality_task_action",
                {
                    "task_id": task_id,
                    "idempotency_key": "new-action-after-resolve",
                    "action": {
                        "kind": "file_write",
                        "path": "out/recovered.txt",
                        "content": "recovered\n",
                    },
                },
                request_id=33,
            )
            self._assert_success(
                action_result,
                action,
                key="new-action-after-resolve",
            )
            self.assertEqual(target.read_text(encoding="utf-8"), "recovered\n")
            self.assertIsNone(action["task"]["pending_operation_id"])
            self.assertEqual(
                len(effect_calls),
                1,
                "the orphan effect must not replay",
            )

    def test_in_process_restart_runs_action_verify_verdict_complete_and_reflect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_server = self._server(root)
            task_id, _ = self._begin(first_server, key="full-begin")

            action_result, action = self._call(
                first_server,
                "causality_task_action",
                {
                    "task_id": task_id,
                    "idempotency_key": "full-action",
                    "action": {
                        "kind": "file_write",
                        "path": "out/result.txt",
                        "content": "durable effect\n",
                    },
                },
                request_id=30,
            )
            self._assert_success(action_result, action, key="full-action")
            self.assertEqual(
                (root / "out" / "result.txt").read_text(encoding="utf-8"),
                "durable effect\n",
            )

            # A new server must reconstruct the task from the ledger, not memory.
            restarted = self._server(root)
            verify_result, verified = self._call(
                restarted,
                "causality_task_verify",
                {
                    "task_id": task_id,
                    "idempotency_key": "full-verify",
                    "requirement_id": "wire-pass",
                    "mode": "execute",
                },
                request_id=31,
            )
            self._assert_success(verify_result, verified, key="full-verify")
            evidence_hash = verified["event_hash"]
            self.assertRegex(evidence_hash, r"^[0-9a-f]{64}$")
            self.assertEqual(verified["data"]["status"], "pass")

            for index, verifier in enumerate(("security", "conformance"), start=1):
                verdict_result, verdict = self._call(
                    restarted,
                    "causality_task_verdict",
                    {
                        "task_id": task_id,
                        "idempotency_key": f"full-verdict-{index}",
                        "verifier": verifier,
                        "status": "pass",
                        "rationale": f"{verifier} independently checked wire-pass",
                        "severity": "normal",
                        "evidence_refs": [evidence_hash],
                    },
                    request_id=31 + index,
                )
                self._assert_success(
                    verdict_result, verdict, key=f"full-verdict-{index}"
                )

            complete_result, completed = self._call(
                restarted,
                "causality_task_complete",
                {"task_id": task_id, "idempotency_key": "full-complete"},
                request_id=34,
            )
            self._assert_success(complete_result, completed, key="full-complete")
            self.assertEqual(completed["task"]["state"], "verified")
            self.assertEqual(completed["data"]["decision"], "pass")

            reflect_arguments = {
                "task_id": task_id,
                "idempotency_key": "full-reflect",
                "scope": f"task:{task_id}",
                "ttl_days": 30,
            }
            reflect_result, reflected = self._call(
                restarted,
                "causality_task_reflect",
                reflect_arguments,
                request_id=35,
            )
            self._assert_success(reflect_result, reflected, key="full-reflect")
            count = restarted.ledger.event_count()
            memory_logs = list(root.rglob("retrospectives/log.jsonl"))
            self.assertEqual(len(memory_logs), 1)
            memory_count = len(
                [
                    line
                    for line in memory_logs[0].read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            )
            self.assertEqual(memory_count, 1)

            replay_result, replay = self._call(
                self._server(root),
                "causality_task_reflect",
                copy.deepcopy(reflect_arguments),
                request_id=36,
            )
            self._assert_success(
                replay_result, replay, key="full-reflect", replayed=True
            )
            self.assertEqual(replay["task"], reflected["task"])
            self.assertEqual(replay["event_hash"], reflected["event_hash"])
            self.assertEqual(replay["data"], reflected["data"])
            self.assertEqual(restarted.ledger.event_count(), count)
            self.assertEqual(
                len(
                    [
                        line
                        for line in memory_logs[0]
                        .read_text(encoding="utf-8")
                        .splitlines()
                        if line.strip()
                    ]
                ),
                memory_count,
            )

    def test_append_evidence_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self._server(temp_dir)
            task_id, _ = self._begin(server, key="evidence-begin")
            result, payload = self._call(
                server,
                "causality_append_evidence",
                {
                    "task_id": task_id,
                    "idempotency_key": "evidence-one",
                    "kind": "tool_output",
                    "payload": {"status": "pass", "summary": "host edit recorded"},
                },
                request_id=40,
            )

            self._assert_success(result, payload, key="evidence-one")
            self.assertRegex(payload["event_hash"], r"^[0-9a-f]{64}$")
            self.assertEqual(payload["task"]["task_id"], task_id)
            self.assertTrue(
                (Path(temp_dir) / ".causality" / "ledger.jsonl").is_file()
            )
            forged_result, forged = self._call(
                server,
                "causality_append_evidence",
                {
                    "task_id": task_id,
                    "idempotency_key": "evidence-forge",
                    "kind": "tool_output",
                    "payload": {
                        "status": "pass",
                        "summary": "must not forge mutation semantics",
                        "mutates_task": True,
                    },
                },
                request_id=41,
            )
            self._assert_domain_error(
                forged_result,
                forged,
                code="validation_error",
            )

    def test_optional_closed_fields_reject_wrong_shapes_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = CausalityMCPServer(temp_dir, approval_token="trusted")
            task_id, _ = self._begin(server, key="shape-begin", risk="high")
            rejected_result, rejected = self._call(
                server,
                "causality_task_approve",
                {
                    "task_id": task_id,
                    "idempotency_key": "shape-reject",
                    "stage": "plan",
                    "approved": False,
                    "approver": "operator",
                    "rationale": "stop this task",
                    "evidence_refs": [],
                    "proof": "trusted",
                },
                request_id=42,
            )
            self._assert_success(rejected_result, rejected, key="shape-reject")
            count = server.ledger.event_count()

            bad_reflect_result, bad_reflect = self._call(
                server,
                "causality_task_reflect",
                {
                    "task_id": task_id,
                    "idempotency_key": "bad-scope",
                    "scope": {"not": "a string"},
                },
                request_id=43,
            )
            self._assert_domain_error(
                bad_reflect_result,
                bad_reflect,
                code="validation_error",
            )
            self.assertEqual(server.ledger.event_count(), count)

            bad_paths_result, bad_paths = self._call(
                server,
                "causality_append_evidence",
                {
                    "task_id": task_id,
                    "idempotency_key": "bad-paths",
                    "kind": "tool_output",
                    "payload": {"status": "pass", "summary": "invalid paths"},
                    "artifact_paths": "ab",
                },
                request_id=44,
            )
            self._assert_domain_error(
                bad_paths_result,
                bad_paths,
                code="validation_error",
            )
            self.assertEqual(server.ledger.event_count(), count)

    def test_concurrent_begin_reports_one_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            servers = (self._server(temp_dir), self._server(temp_dir))
            barrier = threading.Barrier(2)
            for server in servers:
                original = server._begin

                def delayed(arguments, original=original):
                    try:
                        barrier.wait(timeout=0.3)
                    except threading.BrokenBarrierError:
                        pass
                    return original(arguments)

                server._begin = delayed

            arguments = self._begin_arguments(key="concurrent-wire")
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        self._call,
                        server,
                        "causality_task_begin",
                        copy.deepcopy(arguments),
                        request_id=45 + index,
                    )
                    for index, server in enumerate(servers)
                ]
                responses = [future.result() for future in futures]
            payloads = [payload for _result, payload in responses]
            self.assertEqual(
                sorted(item["idempotency"]["replayed"] for item in payloads),
                [False, True],
            )
            self.assertEqual(
                len({item["task"]["task_id"] for item in payloads}),
                1,
            )

    def test_unknown_tool_is_a_json_text_domain_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result, payload = self._call(
                self._server(temp_dir),
                "causality_does_not_exist",
                {},
                request_id=50,
            )
            self._assert_domain_error(result, payload, code="unknown_tool")

    def test_notifications_do_not_receive_responses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self._server(temp_dir)
            self.assertIsNone(
                server.handle(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}
                )
            )
            self.assertIsNone(
                server.handle({"jsonrpc": "2.0", "method": "tools/list"})
            )

    def test_stdio_survives_bad_json_and_invalid_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = Path(__file__).resolve().parents[1] / "src"
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join(
                filter(None, (str(source_root), env.get("PYTHONPATH", "")))
            )
            requests = "\n".join(
                (
                    "{not-json}",
                    "[]",
                    json.dumps(
                        {"jsonrpc": "2.0", "id": 60, "method": "tools/list"}
                    ),
                    json.dumps(
                        {"jsonrpc": "2.0", "method": "notifications/initialized"}
                    ),
                    "",
                )
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "causality.mcp_server",
                    "--project",
                    temp_dir,
                ],
                input=requests,
                text=True,
                capture_output=True,
                check=False,
                timeout=15,
                env=env,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            lines = [line for line in completed.stdout.splitlines() if line.strip()]
            responses = [json.loads(line) for line in lines]
            self.assertEqual(len(responses), 3, completed.stdout)
            self.assertEqual(responses[0]["id"], None)
            self.assertEqual(responses[0]["error"]["code"], -32700)
            self.assertEqual(responses[1]["id"], None)
            self.assertEqual(responses[1]["error"]["code"], -32600)
            self.assertEqual(responses[2]["id"], 60)
            names = {
                tool["name"] for tool in responses[2]["result"]["tools"]
            }
            self.assertTrue(LIFECYCLE_TOOLS.issubset(names))

    def test_init_tool_forwards_safe_activation_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self._server(temp_dir)
            response = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 70,
                    "method": "tools/call",
                    "params": {
                        "name": "causality_init",
                        "arguments": {
                            "client": "generic",
                            "verify": False,
                        },
                    },
                }
            )

            assert response is not None
            result = json.loads(response["result"]["content"][0]["text"])
            self.assertEqual(result["resolved_client"], "generic")
            self.assertEqual(result["activation"], "pending")
            self.assertTrue(
                (Path(temp_dir) / ".causality" / "install-report.json").is_file()
            )

    def test_init_tool_rejects_cli_only_mutation_options(self) -> None:
        for arguments in ({"adopt": True}, {"force": False}):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                agents = root / "AGENTS.md"
                agents.write_text("host rules", encoding="utf-8")
                result, payload = self._call(
                    self._server(root),
                    "causality_init",
                    arguments,
                    request_id=71,
                )

                self._assert_domain_error(result, payload, code="validation_error")
                self.assertIn("CLI-only", payload["error"]["message"])
                self.assertEqual(agents.read_text(encoding="utf-8"), "host rules")

    def test_context_tool_omits_raw_ledger_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self._server(temp_dir)
            sentinel = "context-secret-sentinel"
            server.ledger.append("evidence", {"token": sentinel}, contract_id=sentinel)

            response = server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 72,
                    "method": "tools/call",
                    "params": {"name": "causality_context", "arguments": {"limit": 5}},
                }
            )
            assert response is not None
            text = response["result"]["content"][0]["text"]
            context = json.loads(text)

            self.assertNotIn(sentinel, text)
            self.assertNotIn("payload", context["ledger_tail"][0])
            self.assertNotIn("contract_id", context["ledger_tail"][0])


if __name__ == "__main__":
    unittest.main()
