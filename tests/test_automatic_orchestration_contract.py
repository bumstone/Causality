from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.agent_bootstrap import install_agent_files
from causality.contracts import AuditEventType
from causality.controller import _digest
from causality.mcp_server import CausalityMCPServer
from causality.task_lifecycle import TaskLifecycleError, TaskPolicy


VERIFY_COMMAND = (sys.executable, "-c", "raise SystemExit(0)")


class AutomaticOrchestrationContractTests(unittest.TestCase):
    @staticmethod
    def _server(root: Path) -> CausalityMCPServer:
        return CausalityMCPServer(
            root,
            policy=TaskPolicy(verification_commands=(VERIFY_COMMAND,)),
        )

    def _call(
        self,
        server: CausalityMCPServer,
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        assert response is not None
        result = response["result"]
        return result, json.loads(result["content"][0]["text"])

    @staticmethod
    def _begin_arguments() -> dict[str, Any]:
        return {
            "objective": "implement automatic orchestration with tests",
            "risk": "low",
            "permissions": {
                "allowed_tools": ["file.read", "file.write", "shell"],
                "write_scope": ["out"],
                "network_scope": [],
                "auth_scope": [],
            },
            "verification_requirements": [
                {
                    "id": "pass",
                    "argv": list(VERIFY_COMMAND),
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
            "workflow": "auto",
            "idempotency_key": "begin-auto",
        }

    def _begin(self, server: CausalityMCPServer) -> dict[str, Any]:
        result, payload = self._call(
            server, "causality_task_begin", self._begin_arguments()
        )
        self.assertFalse(result.get("isError", False), payload)
        return payload["task"]

    def test_installer_owns_orchestration_skill_and_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            install_agent_files(root, client="generic")

            skill = (root / "skills" / "causality-orchestrate.md").read_text(
                encoding="utf-8"
            )
            command = (
                root / ".claude" / "commands" / "causality-orchestrate.md"
            ).read_text(encoding="utf-8")
            routing = (root / ".codex" / "causality-routing.md").read_text(
                encoding="utf-8"
            )

            for content in (skill, command, routing):
                self.assertIn("causality-orchestrate", content)
            self.assertIn("causality_init", skill)
            self.assertIn("verify=true", skill)
            self.assertIn("tools/list", skill)
            self.assertIn("controller lease", skill.lower())

    def test_force_refreshes_orchestration_assets_but_preserves_host_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agents = root / "AGENTS.md"
            claude = root / "CLAUDE.md"
            agents.write_text("host codex rules\n", encoding="utf-8")
            claude.write_text("host claude rules\n", encoding="utf-8")
            install_agent_files(root, client="generic")
            skill = root / "skills" / "causality-orchestrate.md"
            command = root / ".claude" / "commands" / "causality-orchestrate.md"
            skill.write_text("stale\n", encoding="utf-8")
            command.write_text("stale\n", encoding="utf-8")

            install_agent_files(root, client="generic", force=True)

            self.assertEqual(agents.read_text(encoding="utf-8"), "host codex rules\n")
            self.assertEqual(claude.read_text(encoding="utf-8"), "host claude rules\n")
            self.assertIn("causality_init", skill.read_text(encoding="utf-8"))
            self.assertIn(
                "causality-orchestrate", command.read_text(encoding="utf-8")
            )

    def test_resume_exposes_one_deterministic_recommended_next_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self._server(Path(temp_dir))
            task = self._begin(server)

            recommendation = task["recommended_next"]
            self.assertEqual(recommendation["operation"], "phase_start")
            self.assertEqual(recommendation["tool"], "causality_task_phase")
            self.assertEqual(recommendation["phase_id"], task["current_phase_id"])
            self.assertIn("reason", recommendation)

    def test_recommended_next_advances_after_action_and_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "out").mkdir()
            server = self._server(root)
            task = self._begin(server)
            task_id = task["task_id"]
            phase_id = task["current_phase_id"]
            lifecycle = server.lifecycle

            lifecycle.phase(
                task_id,
                phase_id=phase_id,
                action="start",
                idempotency_key="progress-start",
            )
            task = lifecycle.action(
                task_id,
                {
                    "kind": "file_write",
                    "path": "out/red-test.txt",
                    "content": "failing check",
                },
                idempotency_key="progress-action",
            )
            self.assertEqual(task.to_dict()["recommended_next"]["operation"], "verify")
            task = lifecycle.verify(
                task_id, "pass", idempotency_key="progress-verify"
            )
            self.assertEqual(task.to_dict()["recommended_next"]["operation"], "verdict")
            verification_hash = task.idempotency[("verify", "progress-verify")].response[
                "event_hash"
            ]

            for index, verifier in enumerate(("correctness", "security"), 1):
                task = lifecycle.verdict(
                    task_id,
                    verifier=verifier,
                    status="pass",
                    rationale=f"{verifier} checked the phase",
                    evidence_refs=(verification_hash,),
                    idempotency_key=f"progress-verdict-{index}",
                )
            recommendation = task.to_dict()["recommended_next"]
            self.assertEqual(recommendation["operation"], "phase_finish")
            self.assertEqual(recommendation["phase_id"], phase_id)
            self.assertGreaterEqual(len(recommendation["evidence_refs"]), 3)

    def test_stale_verification_recommends_verify_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "out").mkdir()
            server = self._server(root)
            arguments = self._begin_arguments()
            arguments["objective"] = "plain task"
            result, payload = self._call(server, "causality_task_begin", arguments)
            self.assertFalse(result.get("isError", False), payload)
            task_id = payload["task"]["task_id"]

            server.lifecycle.verify(
                task_id, "pass", idempotency_key="stale-verify"
            )
            task = server.lifecycle.action(
                task_id,
                {
                    "kind": "file_write",
                    "path": "out/changed.txt",
                    "content": "changed after verification",
                },
                idempotency_key="stale-action",
            )

            recommendation = task.to_dict()["recommended_next"]
            self.assertEqual(recommendation["operation"], "verify")
            self.assertEqual(recommendation["requirement_id"], "pass")

            task = server.lifecycle.verify(
                task_id, "pass", idempotency_key="fresh-verify"
            )
            recommendation = task.to_dict()["recommended_next"]
            self.assertEqual(recommendation["operation"], "verdict")
            refs = tuple(recommendation["evidence_refs"])
            for index, verifier in enumerate(("plain-correctness", "plain-security"), 1):
                task = server.lifecycle.verdict(
                    task_id,
                    verifier=verifier,
                    status="pass",
                    rationale=f"{verifier} checked current evidence",
                    evidence_refs=refs,
                    idempotency_key=f"plain-verdict-{index}",
                )
            self.assertEqual(
                task.to_dict()["recommended_next"]["operation"], "complete"
            )

    def test_conflicting_verifier_review_recommends_fresh_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "out").mkdir()
            server = self._server(root)
            arguments = self._begin_arguments()
            arguments["objective"] = "plain task"
            _, payload = self._call(server, "causality_task_begin", arguments)
            task_id = payload["task"]["task_id"]
            lifecycle = server.lifecycle
            lifecycle.action(
                task_id,
                {"kind": "file_write", "path": "out/work.txt", "content": "work"},
                idempotency_key="conflict-action",
            )
            task = lifecycle.verify(
                task_id, "pass", idempotency_key="conflict-verify"
            )
            refs = tuple(task.to_dict()["recommended_next"]["evidence_refs"])
            for index in (1, 2):
                task = lifecycle.verdict(
                    task_id,
                    verifier="duplicate-reviewer",
                    status="pass",
                    rationale="duplicate review identity",
                    evidence_refs=refs,
                    idempotency_key=f"duplicate-verdict-{index}",
                )

            recommendation = task.to_dict()["recommended_next"]
            self.assertEqual(recommendation["operation"], "verify")
            self.assertEqual(recommendation["requirement_id"], "pass")

    def test_phase_review_reset_uses_only_new_current_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "out").mkdir()
            server = self._server(root)
            task = self._begin(server)
            task_id = task["task_id"]
            phase_id = task["current_phase_id"]
            lifecycle = server.lifecycle
            lifecycle.phase(
                task_id,
                phase_id=phase_id,
                action="start",
                idempotency_key="phase-reset-start",
            )
            lifecycle.action(
                task_id,
                {"kind": "file_write", "path": "out/reset.txt", "content": "work"},
                idempotency_key="phase-reset-action",
            )
            task = lifecycle.verify(
                task_id, "pass", idempotency_key="phase-reset-verify-1"
            )
            old_refs = tuple(task.to_dict()["recommended_next"]["evidence_refs"])
            for index in (1, 2):
                task = lifecycle.verdict(
                    task_id,
                    verifier="duplicate-phase-reviewer",
                    status="pass",
                    rationale="duplicate phase review",
                    evidence_refs=old_refs,
                    idempotency_key=f"phase-reset-duplicate-{index}",
                )
            self.assertEqual(task.to_dict()["recommended_next"]["operation"], "verify")

            task = lifecycle.verify(
                task_id, "pass", idempotency_key="phase-reset-verify-2"
            )
            recommendation = task.to_dict()["recommended_next"]
            current_refs = lifecycle._current_evidence(task)
            self.assertEqual(recommendation["operation"], "verdict")
            self.assertEqual(set(recommendation["evidence_refs"]), set(current_refs))
            self.assertTrue(set(old_refs).isdisjoint(current_refs))

            for index, verifier in enumerate(("reset-correctness", "reset-security"), 1):
                task = lifecycle.verdict(
                    task_id,
                    verifier=verifier,
                    status="pass",
                    rationale=f"{verifier} checked reset evidence",
                    evidence_refs=current_refs,
                    idempotency_key=f"phase-reset-verdict-{index}",
                )
            self.assertEqual(
                task.to_dict()["recommended_next"]["operation"], "phase_finish"
            )

    def test_final_hitl_recommendation_contains_exact_current_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "out").mkdir()
            server = CausalityMCPServer(
                root,
                approval_token="trusted",
                policy=TaskPolicy(verification_commands=(VERIFY_COMMAND,)),
            )
            arguments = self._begin_arguments()
            arguments.update({"objective": "plain task", "risk": "high"})
            _, payload = self._call(server, "causality_task_begin", arguments)
            task_id = payload["task"]["task_id"]
            lifecycle = server.lifecycle
            lifecycle.approve(
                task_id,
                stage="plan",
                approved=True,
                approver="operator",
                rationale="approved high-risk plan",
                evidence_refs=(),
                idempotency_key="final-plan",
                proof="trusted",
            )
            lifecycle.action(
                task_id,
                {"kind": "file_write", "path": "out/high.txt", "content": "work"},
                idempotency_key="final-action",
            )
            task = lifecycle.verify(task_id, "pass", idempotency_key="final-verify")
            refs = tuple(task.to_dict()["recommended_next"]["evidence_refs"])
            for index, verifier in enumerate(("final-correctness", "final-security"), 1):
                lifecycle.verdict(
                    task_id,
                    verifier=verifier,
                    status="pass",
                    rationale=f"{verifier} reviewed current evidence",
                    evidence_refs=refs,
                    idempotency_key=f"final-verdict-{index}",
                )
            task = lifecycle.complete(task_id, idempotency_key="final-complete")

            recommendation = task.to_dict()["recommended_next"]
            self.assertEqual(recommendation["operation"], "approve")
            self.assertEqual(recommendation["approval_stage"], "final")
            self.assertEqual(
                recommendation["evidence_refs"],
                list(task.approval_evidence_refs),
            )
            self.assertTrue(task.approval_evidence_refs)

    def test_active_lease_rejects_unclaimed_and_other_controller_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = self._server(root)
            task = self._begin(server)
            task_id = task["task_id"]
            phase_id = task["current_phase_id"]

            result, claimed = self._call(
                server,
                "causality_task_lease",
                {
                    "task_id": task_id,
                    "controller_id": "controller-a",
                    "action": "acquire",
                    "ttl_seconds": 60,
                    "idempotency_key": "lease-a",
                },
            )
            self.assertFalse(result.get("isError", False), claimed)
            lease = claimed["lease"]

            for controller in (None, "controller-b"):
                arguments = {
                    "task_id": task_id,
                    "phase_id": phase_id,
                    "action": "start",
                    "idempotency_key": f"phase-{controller or 'missing'}",
                }
                if controller is not None:
                    arguments.update(
                        {"controller_id": controller, "lease_id": lease["lease_id"]}
                    )
                denied_result, denied = self._call(
                    server, "causality_task_phase", arguments
                )
                self.assertTrue(denied_result.get("isError"), denied)
                self.assertIn(
                    denied["error"]["code"],
                    {"controller_lease_required", "controller_lease_conflict"},
                )

            ok_result, ok = self._call(
                server,
                "causality_task_phase",
                {
                    "task_id": task_id,
                    "phase_id": phase_id,
                    "action": "start",
                    "idempotency_key": "phase-owner",
                    "controller_id": "controller-a",
                    "lease_id": lease["lease_id"],
                },
            )
            self.assertFalse(ok_result.get("isError", False), ok)

            _, resumed = self._call(
                server, "causality_task_resume", {"task_id": task_id}
            )
            self.assertEqual(resumed["data"]["controller_lease"], lease)

    def test_two_servers_cannot_acquire_the_same_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = self._server(root)
            task_id = self._begin(first)["task_id"]
            second = self._server(root)

            def claim(
                item: tuple[CausalityMCPServer, str]
            ) -> tuple[dict[str, Any], dict[str, Any]]:
                server, controller = item
                return self._call(
                    server,
                    "causality_task_lease",
                    {
                        "task_id": task_id,
                        "controller_id": controller,
                        "action": "acquire",
                        "ttl_seconds": 60,
                        "idempotency_key": f"lease-{controller}",
                    },
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(
                    pool.map(claim, ((first, "controller-a"), (second, "controller-b")))
                )

            successes = [payload for result, payload in outcomes if not result.get("isError")]
            failures = [payload for result, payload in outcomes if result.get("isError")]
            self.assertEqual(len(successes), 1, outcomes)
            self.assertEqual(len(failures), 1, outcomes)
            self.assertEqual(failures[0]["error"]["code"], "controller_lease_conflict")

    def test_two_processes_cannot_acquire_the_same_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            task_id = self._begin(self._server(root))["task_id"]
            program = "\n".join(
                (
                    "import json, sys",
                    "from causality.mcp_server import CausalityMCPServer",
                    "server = CausalityMCPServer(sys.argv[1])",
                    "controller = sys.argv[3]",
                    "response = server.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'causality_task_lease','arguments':{'task_id':sys.argv[2],'controller_id':controller,'action':'acquire','ttl_seconds':60,'idempotency_key':'lease-'+controller}}})",
                    "print(response['result']['content'][0]['text'])",
                )
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", program, str(root), task_id, controller],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=environment,
                )
                for controller in ("process-a", "process-b")
            ]
            payloads = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr)
                payloads.append(json.loads(stdout))

            self.assertEqual(sum(payload.get("ok") is True for payload in payloads), 1)
            self.assertEqual(
                sum(
                    payload.get("error", {}).get("code")
                    == "controller_lease_conflict"
                    for payload in payloads
                ),
                1,
            )
            server = self._server(root)
            self.assertTrue(server.ledger.verify_chain())

    @unittest.skipUnless(os.name == "nt", "Windows process stress job")
    def test_windows_multi_process_lease_stress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            program = "\n".join((
                "import json, sys",
                "from causality.mcp_server import CausalityMCPServer",
                "server=CausalityMCPServer(sys.argv[1])",
                "controller=sys.argv[3]",
                "response=server.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'causality_task_lease','arguments':{'task_id':sys.argv[2],'controller_id':controller,'action':'acquire','ttl_seconds':60,'idempotency_key':'lease-'+controller}}})",
                "print(response['result']['content'][0]['text'])",
            ))
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            for round_number in range(8):
                begin_arguments = self._begin_arguments()
                begin_arguments["objective"] = (
                    f"windows lease stress round {round_number}"
                )
                begin_arguments["idempotency_key"] = (
                    f"begin-windows-stress-{round_number}"
                )
                result, payload = self._call(
                    self._server(root),
                    "causality_task_begin",
                    begin_arguments,
                )
                self.assertFalse(result.get("isError", False), payload)
                task_id = payload["task"]["task_id"]
                processes = [
                    subprocess.Popen(
                        [sys.executable, "-c", program, str(root), task_id,
                         f"stress-{round_number}-{worker}"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, env=environment,
                    )
                    for worker in range(4)
                ]
                payloads = []
                for process in processes:
                    stdout, stderr = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, stderr)
                    payloads.append(json.loads(stdout))
                self.assertEqual(
                    sum(item.get("ok") is True for item in payloads), 1, payloads
                )
                self.assertEqual(sum(
                    item.get("error", {}).get("code") == "controller_lease_conflict"
                    for item in payloads
                ), 3, payloads)
            self.assertTrue(self._server(root).ledger.verify_chain())

    def test_lease_runtime_rejects_invalid_identity_and_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self._server(Path(temp_dir))
            task_id = self._begin(server)["task_id"]
            for controller, key in (("bad controller", "valid"), ("valid", "")):
                result, payload = self._call(
                    server,
                    "causality_task_lease",
                    {
                        "task_id": task_id,
                        "controller_id": controller,
                        "action": "acquire",
                        "ttl_seconds": 60,
                        "idempotency_key": key,
                    },
                )
                self.assertTrue(result.get("isError"), payload)
                self.assertEqual(payload["error"]["code"], "validation_error")

    def test_lease_projection_rejects_semantically_forged_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self._server(Path(temp_dir))
            task_id = self._begin(server)["task_id"]
            now = datetime.now(timezone.utc)
            controller = "forged-controller"
            key = "forged-lease"
            lease_id = str(uuid4())
            request = {
                "task_id": task_id,
                "controller_id": controller,
                "action": "acquire",
                "ttl_seconds": 999,
                "lease_id": None,
            }
            lease = {
                "task_id": task_id,
                "controller_id": controller,
                "lease_id": lease_id,
                "acquired_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=999)).isoformat(),
                "status": "active",
            }
            server.ledger.append(
                AuditEventType.TASK_CONTROLLER_LEASE,
                {
                    "schema_version": 1,
                    "task_id": task_id,
                    "action": "acquire",
                    "controller_id": controller,
                    "lease_id": lease_id,
                    "idempotency_key": key,
                    "request_sha256": _digest(request),
                    "request": request,
                    "response": {"lease": lease},
                },
                contract_id=f"controller:{task_id}",
            )

            with self.assertRaisesRegex(
                TaskLifecycleError, "request digest is invalid"
            ):
                server.controllers.state(task_id)

    def test_lease_replay_release_and_takeover_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = self._server(root)
            task_id = self._begin(server)["task_id"]
            acquire = {
                "task_id": task_id,
                "controller_id": "controller-a",
                "action": "acquire",
                "ttl_seconds": 60,
                "idempotency_key": "lease-a",
            }
            _, first = self._call(server, "causality_task_lease", acquire)
            _, replay = self._call(server, "causality_task_lease", acquire)
            self.assertEqual(first["lease"], replay["lease"])
            self.assertTrue(replay["idempotency"]["replayed"])

            lease_id = first["lease"]["lease_id"]
            _, released = self._call(
                server,
                "causality_task_lease",
                {
                    "task_id": task_id,
                    "controller_id": "controller-a",
                    "lease_id": lease_id,
                    "action": "release",
                    "idempotency_key": "release-a",
                },
            )
            self.assertEqual(released["lease"]["status"], "released")

            denied_result, denied = self._call(
                server,
                "causality_task_complete",
                {"task_id": task_id, "idempotency_key": "after-release"},
            )
            self.assertTrue(denied_result.get("isError"), denied)
            self.assertEqual(denied["error"]["code"], "controller_lease_required")

            takeover = dict(acquire)
            takeover.update(
                {"controller_id": "controller-b", "idempotency_key": "lease-b"}
            )
            result, claimed = self._call(server, "causality_task_lease", takeover)
            self.assertFalse(result.get("isError", False), claimed)
            self.assertEqual(claimed["lease"]["controller_id"], "controller-b")

            stale_result, stale = self._call(
                server, "causality_task_lease", acquire
            )
            self.assertTrue(stale_result.get("isError"), stale)
            self.assertEqual(stale["error"]["code"], "controller_lease_stale")


if __name__ == "__main__":
    unittest.main()
