from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from causality.automatic_orchestration import (
    CheckpointStore,
    InProcessMCPTransport,
    ReferenceOrchestrator,
)
from causality.contracts import AuditEventType, VerifierDecision
from causality.gates import HITLGate
from causality.ledger import EvidenceLedger
from causality.mcp_server import CausalityMCPServer
from causality.orchestration_environment import bounded_environment_snapshot
from causality.task_lifecycle import TaskPolicy
from tests.test_orchestration_driver import VERIFY, _ActiveTransport, contract


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


class OrchestrationHITLTests(unittest.TestCase):
    @staticmethod
    def server(root: str | Path, *, proof: str | None = None) -> CausalityMCPServer:
        return CausalityMCPServer(
            root, approval_token=proof,
            policy=TaskPolicy(verification_commands=(VERIFY,)),
        )

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
            events = [event for event in server.ledger.events(all_segments=True)
                      if event.event_type == AuditEventType.ORCHESTRATION_ENVIRONMENT.value]
            self.assertEqual(len(events), 1)
            payload = json.dumps(events[0].payload, sort_keys=True)
            self.assertNotIn(str(VERIFY), payload)
            self.assertIn(server._policy_digest, payload)


if __name__ == "__main__":
    unittest.main()
