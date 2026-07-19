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
    def __init__(self, activation: str):
        self.activation = activation
        self.calls: list[str] = []

    def tools(self) -> tuple[str, ...]:
        return ("causality_init",)

    def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(name)
        return {"activation": self.activation, "remediation": ["restart client"]}


class OrchestrationDriverTests(unittest.TestCase):
    @staticmethod
    def server(root: str | Path) -> CausalityMCPServer:
        return CausalityMCPServer(
            root, policy=TaskPolicy(verification_commands=(VERIFY,))
        )

    def test_bootstrap_stops_before_work_when_activation_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            transport = _BootstrapTransport("pending")
            driver = ReferenceOrchestrator(
                transport, CheckpointStore(root, "controller-a")
            )
            self.assertEqual(driver.bootstrap().kind, "bootstrap_blocked")
            self.assertEqual(transport.calls, ["causality_init"])

    def test_begin_claim_phase_and_host_handoff_use_real_server(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            driver = ReferenceOrchestrator(
                InProcessMCPTransport(self.server(root)),
                CheckpointStore(root, "controller-a"),
            )
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
            first = ReferenceOrchestrator(lossy, store).begin(contract())
            self.assertIsInstance(first, DriverDirective)
            assert isinstance(first, DriverDirective)
            self.assertEqual(first.kind, "recovery_required")
            self.assertEqual(store.load().status, "prepared")
            self.assertIsInstance(
                ReferenceOrchestrator(lossy, store).begin(contract()), dict
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
            result = ReferenceOrchestrator(
                InProcessMCPTransport(server), store
            ).begin(contract())
            self.assertIsInstance(result, DriverDirective)
            assert isinstance(result, DriverDirective)
            self.assertEqual(result.kind, "recovery_required")
            self.assertEqual(server.ledger.event_count(), 0)

    def test_host_action_is_checkpointed_then_driver_runs_verification(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            driver = ReferenceOrchestrator(
                InProcessMCPTransport(self.server(root)),
                CheckpointStore(root, "controller-a"),
            )
            begun = driver.begin(contract())
            assert isinstance(begun, dict)
            task_id = begun["task"]["task_id"]
            submitted = driver.submit_host_action(task_id, {"action": {
                "kind": "file_write", "path": "out/driver.txt",
                "content": "host-selected content",
            }})
            self.assertEqual(submitted.kind, "advanced")
            self.assertEqual(driver.advance(task_id).kind, "verifier_required")


if __name__ == "__main__":
    unittest.main()
