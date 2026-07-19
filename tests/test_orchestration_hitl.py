from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from causality.automatic_orchestration import (
    CheckpointStore,
    InProcessMCPTransport,
    ReferenceOrchestrator,
)
from causality.contracts import AuditEventType, GoalContract, VerifierDecision
from causality.gates import HITLGate
from causality.ledger import EvidenceLedger
from causality.mcp_server import CausalityMCPServer
from causality.orchestration_environment import bounded_environment_snapshot
from causality.task_lifecycle import TaskLifecycleError, TaskPolicy


VERIFY = (sys.executable, "-c", "print('driver-pass')")
class _FutureDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(seconds=10)


def contract() -> dict[str, Any]:
    return {
        "objective": "exercise orchestration HITL", "risk": "low",
        "permissions": {"allowed_tools": ["file.read", "file.write", "shell"],
                        "write_scope": ["out"], "network_scope": [], "auth_scope": []},
        "verification_requirements": [{
            "id": "driver-pass", "argv": list(VERIFY), "expected_exit_codes": [0],
            "timeout_seconds": 30, "artifact_paths": {}, "required": True, "manual": False,
        }], "non_goals": ["write outside the project"], "workflow": "auto",
        "stop_condition": {"max_iterations": 8, "max_failed_hypotheses": 3,
                           "no_progress_iterations": 2},
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


class _ApprovalResponseLoss:
    def __init__(self, delegate: InProcessMCPTransport):
        self.delegate = delegate
        self.failed = False
        self.calls: list[str] = []
    def tools(self) -> tuple[str, ...]:
        return self.delegate.tools()
    def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(name)
        result = self.delegate.call(name, arguments)
        if name == "causality_task_approve" and not self.failed:
            self.failed = True
            raise ConnectionError("response lost")
        return result


class _ResponseLoss:
    def __init__(self, delegate: InProcessMCPTransport, tool: str, *, before=False):
        self.delegate = delegate
        self.tool = tool
        self.before = before
        self.failed = False
    def tools(self) -> tuple[str, ...]:
        return self.delegate.tools()
    def call(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.before and name == self.tool and not self.failed:
            self.failed = True
            raise ConnectionError("request not delivered")
        result = self.delegate.call(name, arguments)
        if not self.before and name == self.tool and not self.failed:
            self.failed = True
            raise ConnectionError("response lost")
        return result


class OrchestrationHITLTests(unittest.TestCase):
    @staticmethod
    def server(root: str | Path, *, proof: str | None = None) -> CausalityMCPServer:
        return CausalityMCPServer(
            root, approval_token=proof, policy=TaskPolicy(verification_commands=(VERIFY,)),
        )

    @staticmethod
    def environment_events(server: CausalityMCPServer):
        return [event for event in server.ledger.events(all_segments=True) if
                event.event_type == AuditEventType.ORCHESTRATION_ENVIRONMENT.value]

    def test_uncertain_hitl_proof_is_not_persisted_or_automatically_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            proof = "operator-proof-must-never-persist"
            server = self.server(root, proof=proof)
            transport = _ApprovalResponseLoss(InProcessMCPTransport(server))
            store = CheckpointStore(root, "controller-a")
            driver = ReferenceOrchestrator(_ActiveTransport(transport, root), store)
            self.assertEqual(driver.bootstrap().kind, "ready")
            request = contract()
            request["risk"] = "high"
            begun = driver.begin(request)
            assert isinstance(begun, dict)
            task_id = begun["task"]["task_id"]
            self.assertEqual(driver.step(task_id).kind, "human_input_required")
            decision = {
                "stage": "plan", "approved": True, "approver": "operator",
                "rationale": "scope reviewed", "evidence_refs": [],
            }
            self.assertEqual(
                driver.submit_human(task_id, decision, proof=proof).kind,
                "human_input_required",
            )
            self.assertNotIn(proof, store.path.read_text(encoding="utf-8"))
            self.assertNotIn(proof, server.ledger.path.read_text(encoding="utf-8"))
            self.assertEqual(
                driver.submit_human(task_id, decision, proof=proof).kind, "blocked"
            )
            self.assertEqual(transport.calls.count("causality_task_approve"), 1)

    def test_same_provider_cannot_satisfy_orchestrated_quorum(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            ledger = EvidenceLedger(Path(root) / "ledger.jsonl")
            evidence_hash = "a" * 64
            for verifier in ("code-review", "security-review"):
                ledger.append(AuditEventType.VERIFIER_DECISION, {
                    **VerifierDecision(
                        verifier, "pass", "review passed",
                        evidence_refs=(evidence_hash,),
                    ).to_dict(),
                    "provider_id": "same-provider", "orchestrated": True,
                }, contract_id="task-a")
            issues = HITLGate(ledger)._structured_verifier_issues(
                ledger.events(all_segments=True), None, review_after=-1,
                requirement_hashes={evidence_hash}, min_passes=2,
            )
            self.assertTrue(any("duplicate verifier providers" in item for item in issues))

    def test_legacy_verdict_cannot_complete_an_orchestrated_quorum(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            ledger = EvidenceLedger(Path(root) / "ledger.jsonl")
            evidence_hash = "a" * 64
            for verifier, metadata in (
                ("orchestrated", {"provider_id": "provider-a", "orchestrated": True}),
                ("legacy", {}),
            ):
                ledger.append(AuditEventType.VERIFIER_DECISION, {
                    **VerifierDecision(
                        verifier, "pass", "review passed",
                        evidence_refs=(evidence_hash,),
                    ).to_dict(), **metadata,
                }, contract_id="task-a")
            issues = HITLGate(ledger)._structured_verifier_issues(
                ledger.events(all_segments=True), None, review_after=-1,
                requirement_hashes={evidence_hash}, min_passes=2,
            )
            self.assertIn(
                "orchestrated verifier provider identity must be non-blank", issues
            )

    def test_two_provider_success_reflects_and_releases_controller(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = self.server(root)
            driver = ReferenceOrchestrator(
                _ActiveTransport(InProcessMCPTransport(server), root),
                CheckpointStore(root, "controller-a"),
            )
            self.assertEqual(driver.bootstrap().kind, "ready")
            begun = driver.begin(contract())
            assert isinstance(begun, dict)
            task_id = begun["task"]["task_id"]
            driver.submit_host_action(task_id, {"action": {
                "kind": "file_write", "path": "out/success.txt",
                "content": "verified",
            }})
            handoff = driver.advance(task_id)
            refs = tuple(handoff.details["evidence_refs"])
            lease = server.controllers.state(task_id)
            assert lease is not None
            with self.assertRaises(TaskLifecycleError) as missing_provider:
                server._lifecycle_call("causality_task_verdict", {
                    "task_id": task_id,
                    "controller_id": "controller-a",
                    "lease_id": lease["lease_id"],
                    "verifier": "bypass-attempt",
                    "status": "pass",
                    "rationale": "provider omitted",
                    "evidence_refs": list(refs),
                    "idempotency_key": "missing-provider",
                })
            self.assertEqual(missing_provider.exception.code, "validation_error")
            for verifier, provider in (
                ("code-review", "provider-a"),
                ("security-review", "provider-b"),
            ):
                self.assertEqual(driver.submit_verifier(
                    task_id, verifier_id=verifier, provider_id=provider,
                    status="pass", rationale="independent review passed",
                    evidence_refs=refs,
                ).kind, "advanced")
                if provider == "provider-a":
                    self.assertEqual(driver.advance(task_id).kind, "verifier_required")
            self.assertEqual(driver.advance(task_id).kind, "terminal")
            self.assertEqual(driver.advance(task_id).kind, "terminal")
            self.assertIsNotNone(server.lifecycle.get(task_id).reflection)
            self.assertEqual(server.controllers.state(task_id)["status"], "released")

    def test_manual_verification_is_routed_to_hitl(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            proof = "trusted-operator-proof"
            server = self.server(root, proof=proof)
            driver = ReferenceOrchestrator(
                _ActiveTransport(InProcessMCPTransport(server), root),
                CheckpointStore(root, "controller-a"),
            )
            self.assertEqual(driver.bootstrap().kind, "ready")
            request = contract()
            request["verification_requirements"] = [{
                "id": "visual", "argv": [], "expected_exit_codes": [0],
                "timeout_seconds": 30, "artifact_paths": {},
                "required": True, "manual": True,
            }]
            begun = driver.begin(request)
            assert isinstance(begun, dict)
            task_id = begun["task"]["task_id"]
            action = driver.submit_host_action(task_id, {"action": {
                "kind": "file_write", "path": "out/manual.txt",
                "content": "review me",
            }})
            waiting = driver.advance(task_id)
            self.assertEqual(waiting.kind, "human_input_required")
            self.assertEqual(waiting.details["requirement_id"], "visual")
            evidence_hash = next(
                event.entry_hash
                for event in reversed(
                    server.ledger.events_for_contract(task_id, all_segments=True)
                )
                if event.event_type == AuditEventType.EVIDENCE.value
            )
            mismatch = driver.submit_human(task_id, {
                "requirement_id": "another-requirement",
                "evidence_hash": evidence_hash,
                "approved": True,
                "approver": "operator",
                "rationale": "wrong requirement",
            }, proof=proof)
            self.assertEqual(mismatch.kind, "blocked")
            decided = driver.submit_human(task_id, {
                "evidence_hash": evidence_hash,
                "approved": True,
                "approver": "operator",
                "rationale": "visual result reviewed",
            }, proof=proof)
            self.assertEqual(decided.kind, "advanced")
            self.assertEqual(driver.advance(task_id).kind, "verifier_required")

    def test_lost_verdict_response_replays_one_provider_decision(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = self.server(root)
            lossy = _ResponseLoss(
                InProcessMCPTransport(server), "causality_task_verdict"
            )
            driver = ReferenceOrchestrator(
                _ActiveTransport(lossy, root),
                CheckpointStore(root, "controller-a"),
            )
            self.assertEqual(driver.bootstrap().kind, "ready")
            begun = driver.begin(contract())
            assert isinstance(begun, dict)
            task_id = begun["task"]["task_id"]
            driver.submit_host_action(task_id, {"action": {
                "kind": "file_write", "path": "out/lost-verdict.txt",
                "content": "reviewed",
            }})
            handoff = driver.advance(task_id)
            refs = tuple(handoff.details["evidence_refs"])
            first = driver.submit_verifier(
                task_id, verifier_id="code-review", provider_id="provider-a",
                status="pass", rationale="reviewed", evidence_refs=refs,
            )
            self.assertEqual(first.kind, "recovery_required")
            second = driver.submit_verifier(
                task_id, verifier_id="code-review", provider_id="provider-a",
                status="pass", rationale="reviewed", evidence_refs=refs,
            )
            self.assertEqual(second.kind, "advanced")
            decisions = [
                event for event in server.ledger.events_for_contract(
                    task_id, all_segments=True
                )
                if event.event_type == AuditEventType.VERIFIER_DECISION.value
                and event.payload.get("provider_id") == "provider-a"
            ]
            self.assertEqual(len(decisions), 1)

    def test_lease_environment_record_is_bounded_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            snapshot = bounded_environment_snapshot(
                root, ("causality_task_resume", "causality_task_lease"), "b" * 64
            )
            self.assertEqual(set(snapshot), {
                "causality_version", "python_version", "os", "capabilities",
                "capabilities_sha256", "policy_sha256", "git",
            })
            server = self.server(root)
            driver = ReferenceOrchestrator(
                _ActiveTransport(InProcessMCPTransport(server), root),
                CheckpointStore(root, "controller-a"),
            )
            self.assertEqual(driver.bootstrap().kind, "ready")
            begun = driver.begin(contract())
            assert isinstance(begun, dict)
            events = self.environment_events(server)
            self.assertEqual(len(events), 1)
            payload = json.dumps(events[0].payload, sort_keys=True)
            self.assertNotIn(str(VERIFY), payload)
            self.assertIn(server._policy_digest, payload)

    def test_lease_replay_backfills_environment_after_append_crash(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = self.server(root)
            task = server.lifecycle.begin(
                GoalContract(title="environment recovery", summary=""),
                idempotency_key="environment-task",
            )
            arguments = {
                "task_id": task.task_id,
                "controller_id": "controller-a",
                "action": "acquire",
                "ttl_seconds": 60,
                "idempotency_key": "environment-lease",
            }
            original_append = server.ledger.append

            def crash_environment(event_type, *args, **kwargs):
                value = event_type.value if isinstance(event_type, AuditEventType) else event_type
                if value == AuditEventType.ORCHESTRATION_ENVIRONMENT.value:
                    raise OSError("environment append interrupted")
                return original_append(event_type, *args, **kwargs)

            with patch.object(server.ledger, "append", side_effect=crash_environment):
                with self.assertRaises(OSError):
                    server._lease(arguments)

            replay = server._lease(arguments)
            self.assertIn("\"replayed\": true", replay["content"][0]["text"])
            records = self.environment_events(server)
            self.assertEqual(len(records), 1)
            server._lease(arguments)
            records = self.environment_events(server)
            self.assertEqual(len(records), 1)

    def test_driver_resume_replays_lease_before_acknowledging_audit(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = self.server(root)
            driver = ReferenceOrchestrator(
                _ActiveTransport(InProcessMCPTransport(server), root),
                CheckpointStore(root, "controller-a"),
            )
            self.assertEqual(driver.bootstrap().kind, "ready")
            original_append = server.ledger.append

            def crash_environment(event_type, *args, **kwargs):
                value = event_type.value if isinstance(event_type, AuditEventType) else event_type
                if value == AuditEventType.ORCHESTRATION_ENVIRONMENT.value:
                    raise OSError("environment append interrupted")
                return original_append(event_type, *args, **kwargs)

            with patch.object(server.ledger, "append", side_effect=crash_environment):
                begun = driver.begin(contract())
            self.assertEqual(begun.kind, "recovery_required")
            checkpoint = driver.checkpoints.load()
            assert checkpoint is not None and checkpoint.task_id is not None
            task_id = checkpoint.task_id
            self.assertFalse(any(
                event.event_type == AuditEventType.ORCHESTRATION_ENVIRONMENT.value
                for event in server.ledger.events(all_segments=True)
            ))
            self.assertEqual(driver.step(task_id).kind, "host_action_required")
            records = self.environment_events(server)
            self.assertEqual(len(records), 1)

    def test_expired_lease_replay_backfills_audit_before_stale_error(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = self.server(root)
            driver = ReferenceOrchestrator(
                _ActiveTransport(InProcessMCPTransport(server), root),
                CheckpointStore(root, "controller-a"),
                lease_seconds=5,
            )
            self.assertEqual(driver.bootstrap().kind, "ready")
            original_append = server.ledger.append

            def crash_environment(event_type, *args, **kwargs):
                value = event_type.value if isinstance(event_type, AuditEventType) else event_type
                if value == AuditEventType.ORCHESTRATION_ENVIRONMENT.value:
                    raise OSError("environment append interrupted")
                return original_append(event_type, *args, **kwargs)

            with patch.object(server.ledger, "append", side_effect=crash_environment):
                begun = driver.begin(contract())
            self.assertEqual(begun.kind, "recovery_required")
            checkpoint = driver.checkpoints.load()
            assert checkpoint is not None and checkpoint.task_id is not None
            with patch("causality.controller.datetime", _FutureDateTime):
                stale = driver.step(checkpoint.task_id)
            self.assertEqual(stale.kind, "blocked")
            self.assertEqual(stale.details["error"]["code"], "controller_lease_stale")
            records = self.environment_events(server)
            self.assertEqual(len(records), 1)

    def test_driver_resume_refreshes_state_after_undelivered_lease(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            server = self.server(root)
            lossy = _ResponseLoss(
                InProcessMCPTransport(server), "causality_task_lease", before=True
            )
            driver = ReferenceOrchestrator(
                _ActiveTransport(lossy, root),
                CheckpointStore(root, "controller-a"),
            )
            self.assertEqual(driver.bootstrap().kind, "ready")
            begun = driver.begin(contract())
            self.assertEqual(begun.kind, "recovery_required")
            checkpoint = driver.checkpoints.load()
            assert checkpoint is not None and checkpoint.task_id is not None
            self.assertIsNone(server.controllers.state(checkpoint.task_id))
            recovered = driver.step(checkpoint.task_id)
            self.assertEqual(recovered.kind, "host_action_required")
            lease = server.controllers.state(checkpoint.task_id)
            assert lease is not None
            self.assertEqual(lease["status"], "active")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is POSIX-only")
    def test_git_metadata_fifo_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            git_dir = Path(root) / ".git"
            git_dir.mkdir()
            os.mkfifo(git_dir / "HEAD")
            snapshot = bounded_environment_snapshot(root, (), "c" * 64)
            self.assertIsNone(snapshot["git"]["head"])


if __name__ == "__main__":
    unittest.main()
