from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.automatic_orchestration import (
    CheckpointStore,
    DriverDirective,
    InProcessMCPTransport,
    OrchestrationCheckpoint,
    ReferenceOrchestrator,
)
from causality.contracts import AuditEventType
from causality.mcp_server import CausalityMCPServer
from causality.task_lifecycle import TaskPolicy


VERIFY = (sys.executable, "-c", "print('driver-pass')")
FAIL_VERIFY = (sys.executable, "-c", "raise SystemExit(9)")
TIMEOUT_VERIFY = (sys.executable, "-c", "import time; time.sleep(1)")


def contract(workflow: str = "auto") -> dict[str, Any]:
    return {
        "objective": "exercise the reference orchestration driver",
        "risk": "low",
        "permissions": {
            "allowed_tools": ["file.read", "file.write", "shell"],
            "write_scope": ["out"], "network_scope": [], "auth_scope": [],
        },
        "verification_requirements": [{
            "id": "driver-pass", "argv": list(VERIFY),
            "expected_exit_codes": [0], "timeout_seconds": 30,
            "artifact_paths": {}, "required": True, "manual": False,
        }],
        "stop_condition": {
            "max_iterations": 8, "max_failed_hypotheses": 3,
            "no_progress_iterations": 2,
        },
        "non_goals": ["write outside the project"],
        "workflow": workflow,
    }


class _LossyTransport:
    def __init__(self, delegate: InProcessMCPTransport, fail_tool: str):
        self.delegate = delegate
        self.fail_tool = fail_tool
        self.failed = False

    def tools(self) -> tuple[str, ...]:
        return self.delegate.tools()

    def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self.delegate.call(name, arguments)
        if name == self.fail_tool and not self.failed:
            self.failed = True
            raise ConnectionError("response lost after durable mutation")
        return result


class _BootstrapTransport:
    def __init__(self, activation: str, project_root: str | Path):
        self.activation = activation
        self.project_root = str(Path(project_root).resolve())
        self.calls: list[str] = []

    def tools(self) -> tuple[str, ...]:
        return ("causality_init",)

    def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(name)
        return {
            "activation": self.activation,
            "project_root": self.project_root,
            "remediation": ["restart client"],
        }


class _ActiveTransport:
    def __init__(self, delegate: InProcessMCPTransport, project_root: str | Path):
        self.delegate = delegate
        self.project_root = str(Path(project_root).resolve())

    def tools(self) -> tuple[str, ...]:
        return self.delegate.tools()

    def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if name == "causality_init":
            return {"activation": "active", "project_root": self.project_root}
        return self.delegate.call(name, arguments)


class OrchestrationDriverTests(unittest.TestCase):
    @staticmethod
    def server(root: str | Path) -> CausalityMCPServer:
        return CausalityMCPServer(
            root, policy=TaskPolicy(verification_commands=(VERIFY,))
        )

    def driver(
        self, root: str | Path, transport: Any | None = None
    ) -> ReferenceOrchestrator:
        selected = transport or InProcessMCPTransport(self.server(root))
        if not isinstance(selected, _ActiveTransport):
            selected = _ActiveTransport(selected, root)
        driver = ReferenceOrchestrator(
            selected, CheckpointStore(root, "controller-a")
        )
        self.assertEqual(driver.bootstrap().kind, "ready")
        return driver

    def test_bootstrap_stops_before_work_when_activation_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            transport = _BootstrapTransport("pending", root)
            driver = ReferenceOrchestrator(
                transport, CheckpointStore(root, "controller-a")
            )
            self.assertEqual(driver.bootstrap().kind, "bootstrap_blocked")
            self.assertEqual(transport.calls, ["causality_init"])

    def test_work_and_cross_project_bootstrap_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
            transport = _BootstrapTransport("active", other)
            driver = ReferenceOrchestrator(
                transport, CheckpointStore(root, "controller-a")
            )
            self.assertEqual(driver.begin(contract()).kind, "bootstrap_required")
            self.assertEqual(driver.bootstrap().kind, "bootstrap_blocked")
            self.assertEqual(driver.begin(contract()).kind, "bootstrap_required")

    def test_begin_claim_phase_and_host_handoff_use_real_server(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            driver = self.driver(root)
            begun = driver.begin(contract("root-cause-protocol"))
            self.assertIsInstance(begun, dict)
            assert isinstance(begun, dict)
            task_id = begun["task"]["task_id"]
            self.assertEqual(begun["lease"]["controller_id"], "controller-a")
            self.assertEqual(driver.step(task_id).operation, "phase_start")
            self.assertEqual(driver.advance(task_id).kind, "host_action_required")

    def test_lost_begin_response_replays_exact_request_once(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = self.server(root)
            lossy = _LossyTransport(
                InProcessMCPTransport(server), "causality_task_begin"
            )
            store = CheckpointStore(root, "controller-a")
            first_driver = ReferenceOrchestrator(_ActiveTransport(lossy, root), store)
            self.assertEqual(first_driver.bootstrap().kind, "ready")
            first = first_driver.begin(contract())
            self.assertIsInstance(first, DriverDirective)
            assert isinstance(first, DriverDirective)
            self.assertEqual(first.kind, "recovery_required")
            self.assertEqual(store.load().status, "prepared")
            self.assertIsInstance(
                self.driver(root, lossy).begin(contract()), dict
            )
            starts = [
                event for event in server.ledger.events(all_segments=True)
                if event.event_type == AuditEventType.TASK_STARTED.value
            ]
            self.assertEqual(len(starts), 1)

    def test_conflicting_prepared_mutation_stops_before_server_write(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = self.server(root)
            store = CheckpointStore(root, "controller-a")
            store.save(OrchestrationCheckpoint(
                controller_id="controller-a", operation="causality_task_phase",
                idempotency_key="phase-a", request_sha256="0" * 64,
                status="prepared", task_id="another-task",
            ))
            active = _ActiveTransport(InProcessMCPTransport(server), root)
            driver = ReferenceOrchestrator(active, store)
            self.assertEqual(driver.bootstrap().kind, "ready")
            result = driver.begin(contract())
            self.assertIsInstance(result, DriverDirective)
            assert isinstance(result, DriverDirective)
            self.assertEqual(result.kind, "recovery_required")
            self.assertEqual(server.ledger.event_count(), 0)

    def test_host_action_is_checkpointed_then_driver_runs_verification(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            driver = self.driver(root)
            begun = driver.begin(contract())
            assert isinstance(begun, dict)
            task_id = begun["task"]["task_id"]
            submitted = driver.submit_host_action(task_id, {"action": {
                "kind": "file_write", "path": "out/driver.txt",
                "content": "host-selected content",
            }})
            self.assertEqual(submitted.kind, "advanced")
            self.assertEqual(driver.advance(task_id).kind, "verifier_required")

    def test_response_loss_converges_after_phase_action_and_verify(self) -> None:
        for tool, workflow in (
            ("causality_task_phase", "root-cause-protocol"),
            ("causality_task_action", "auto"),
            ("causality_task_verify", "auto"),
        ):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as root:
                server = self.server(root)
                lossy = _LossyTransport(InProcessMCPTransport(server), tool)
                driver = self.driver(root, lossy)
                begun = driver.begin(contract(workflow))
                assert isinstance(begun, dict)
                task_id = begun["task"]["task_id"]
                if tool == "causality_task_phase":
                    lost = driver.step(task_id)
                    expected = "host_action_required"
                else:
                    lost = driver.submit_host_action(task_id, {"action": {
                        "kind": "file_write", "path": "out/lost.txt",
                        "content": "applied once",
                    }})
                    if tool == "causality_task_verify":
                        self.assertEqual(lost.kind, "advanced")
                        lost = driver.step(task_id)
                    expected = "verifier_required"
                self.assertEqual(lost.kind, "recovery_required")
                recovered = self.driver(root, lossy).advance(task_id)
                self.assertEqual(recovered.kind, expected)

    def test_released_lease_uses_a_new_acquire_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = self.server(root)
            driver = self.driver(root, InProcessMCPTransport(server))
            begun = driver.begin(contract())
            assert isinstance(begun, dict)
            task_id = begun["task"]["task_id"]
            first = begun["lease"]
            released = driver.release(
                task_id, first["lease_id"],
                last_event_hash=begun["task"]["latest_event_hash"],
            )
            self.assertIsInstance(released, dict)
            self.assertEqual(driver.step(task_id).kind, "host_action_required")
            second = server.controllers.state(task_id)
            self.assertEqual(second["status"], "active")
            self.assertNotEqual(second["lease_id"], first["lease_id"])

    def test_failed_verification_stops_before_automatic_retry(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = CausalityMCPServer(
                root,
                policy=TaskPolicy(verification_commands=(VERIFY, FAIL_VERIFY)),
            )
            driver = self.driver(root, InProcessMCPTransport(server))
            request = contract()
            request["verification_requirements"][0]["argv"] = list(FAIL_VERIFY)
            begun = driver.begin(request)
            assert isinstance(begun, dict)
            task_id = begun["task"]["task_id"]
            driver.submit_host_action(task_id, {"action": {
                "kind": "file_write", "path": "out/failure.txt", "content": "failed",
            }})
            stopped = driver.advance(task_id)
            self.assertEqual(stopped.kind, "verification_failed")
            self.assertEqual(stopped.details["status"], "fail")
            self.assertEqual(driver.advance(task_id).kind, "verification_failed")
            results = [
                event for event in server.ledger.events_for_contract(
                    task_id, all_segments=True
                )
                if event.event_type == AuditEventType.EVIDENCE.value
                and event.payload.get("requirement_id") == "driver-pass"
            ]
            self.assertEqual(len(results), 1)

    def test_lost_timeout_response_resumes_as_verification_failure(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = CausalityMCPServer(
                root,
                policy=TaskPolicy(
                    verification_commands=(VERIFY, TIMEOUT_VERIFY),
                ),
            )
            lossy = _LossyTransport(
                InProcessMCPTransport(server), "causality_task_verify"
            )
            driver = self.driver(root, lossy)
            request = contract()
            requirement = request["verification_requirements"][0]
            requirement["argv"] = list(TIMEOUT_VERIFY)
            requirement["timeout_seconds"] = 0.01
            begun = driver.begin(request)
            assert isinstance(begun, dict)
            task_id = begun["task"]["task_id"]
            driver.submit_host_action(task_id, {"action": {
                "kind": "file_write", "path": "out/timeout.txt", "content": "x",
            }})
            self.assertEqual(driver.advance(task_id).kind, "recovery_required")
            resumed = self.driver(root, lossy)
            self.assertEqual(resumed.advance(task_id).kind, "verification_failed")
            self.assertEqual(resumed.advance(task_id).kind, "verification_failed")
            results = [
                event for event in server.ledger.events_for_contract(
                    task_id, all_segments=True
                )
                if event.event_type == AuditEventType.EVIDENCE.value
                and event.payload.get("requirement_id") == "driver-pass"
            ]
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].payload["status"], "timeout")


if __name__ == "__main__":
    unittest.main()
