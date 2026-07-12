from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import (
    AuditEventType,
    GoalContract,
    PermissionContract,
    Risk,
    VerificationRequirement,
)
from causality.ledger import EvidenceLedger
from causality.task_lifecycle import (
    TaskLifecycle,
    TaskLifecycleError,
    TaskPolicy,
    TaskState,
    canonical_sha256,
)


class SimulatedProcessDeath(BaseException):
    """Stop between durable appends without running normal recovery code."""


class TaskLifecycleAtomicityTests(unittest.TestCase):
    @staticmethod
    def _action(content: str) -> dict[str, object]:
        return {
            "kind": "file_write",
            "path": "out/result.txt",
            "content": content,
        }

    def _completion_ready(
        self,
        root: Path,
        effects: list[dict[str, object]],
    ) -> tuple[TaskLifecycle, TaskPolicy, str]:
        command = (sys.executable, "-c", "print('atomic completion')")
        policy = TaskPolicy(verification_commands=(command,))
        lifecycle = TaskLifecycle(
            root,
            policy=policy,
            effect_runner=lambda action: effects.append(action) or {"status": "ok"},
        )
        task = lifecycle.begin(
            GoalContract(
                title="atomic completion",
                summary="completion must be bound to the reviewed task state",
                permissions=PermissionContract(
                    allowed_tools=("shell", "file.write"),
                    write_scope=("out",),
                ),
                verification_requirements=(
                    VerificationRequirement(id="unit", argv=command),
                ),
            ),
            idempotency_key="begin",
        )
        task = lifecycle.verify(task.task_id, "unit", idempotency_key="verify")
        evidence_hash = task.idempotency[("verify", "verify")].response[
            "event_hash"
        ]
        for index, verifier in enumerate(("security", "conformance"), start=1):
            task = lifecycle.verdict(
                task.task_id,
                verifier=verifier,
                status="pass",
                rationale="reviewed the current verification evidence",
                evidence_refs=(evidence_hash,),
                idempotency_key=f"verdict-{index}",
            )
        return lifecycle, policy, task.task_id

    def _escalation_ready(
        self,
        root: Path,
        effects: list[dict[str, object]],
    ) -> tuple[TaskLifecycle, TaskPolicy, str]:
        command = (sys.executable, "-c", "print('ready for final approval')")
        policy = TaskPolicy(verification_commands=(command,))
        lifecycle = TaskLifecycle(
            root,
            policy=policy,
            approval_authorizer=lambda _approver, _stage, _proof: True,
            effect_runner=lambda action: effects.append(action) or {"status": "ok"},
        )
        task = lifecycle.begin(
            GoalContract(
                title="partial completion escalation",
                summary="an unfinished escalation must stop later work",
                risk=Risk.HIGH,
                permissions=PermissionContract(
                    allowed_tools=("file.write", "shell"),
                    write_scope=("out",),
                ),
                verification_requirements=(
                    VerificationRequirement(id="ready", argv=command),
                ),
            ),
            idempotency_key="begin-escalation",
        )
        task = lifecycle.approve(
            task.task_id,
            stage="plan",
            approved=True,
            approver="operator",
            rationale="plan reviewed",
            idempotency_key="approve-plan",
            proof="trusted",
        )
        task = lifecycle.action(
            task.task_id,
            self._action("initial"),
            idempotency_key="initial-action",
        )
        task = lifecycle.verify(
            task.task_id,
            "ready",
            idempotency_key="verify-ready",
        )
        evidence_hash = task.idempotency[("verify", "verify-ready")].response[
            "event_hash"
        ]
        for index, verifier in enumerate(("one", "two"), start=1):
            task = lifecycle.verdict(
                task.task_id,
                verifier=verifier,
                status="pass",
                rationale="current evidence reviewed",
                evidence_refs=(evidence_hash,),
                idempotency_key=f"verdict-{index}",
            )
        return lifecycle, policy, task.task_id

    @staticmethod
    def _completion_gate_events(root: Path, task_id: str):
        return [
            event
            for event in EvidenceLedger(
                root / ".causality" / "ledger.jsonl"
            ).events_for_contract(task_id, all_segments=True)
            if event.event_type == "gate_decision"
            and event.payload.get("operation") == "complete"
            and event.payload.get("idempotency_key") == "complete"
        ]

    def test_completion_retry_revalidates_after_crash_before_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            effects: list[dict[str, object]] = []
            lifecycle, policy, task_id = self._completion_ready(root, effects)

            with patch.object(
                lifecycle,
                "_append_operation",
                side_effect=SimulatedProcessDeath(
                    "completion gate durable, operation not durable"
                ),
            ):
                with self.assertRaises(SimulatedProcessDeath):
                    lifecycle.complete(task_id, idempotency_key="complete")

            gates = self._completion_gate_events(root, task_id)
            self.assertEqual(len(gates), 1)
            self.assertEqual(gates[0].payload["decision"], "pass")

            fresh = TaskLifecycle(
                root,
                policy=policy,
                effect_runner=lambda action: effects.append(action) or {"status": "ok"},
            )
            fresh.action(
                task_id,
                self._action("changed after the gate"),
                idempotency_key="post-gate-mutation",
            )
            with self.assertRaises(TaskLifecycleError) as stale:
                fresh.complete(task_id, idempotency_key="complete")
            self.assertEqual(stale.exception.code, "completion_snapshot_stale")
            self.assertEqual(fresh.get(task_id).state, TaskState.EXECUTING)

    def test_completion_retry_revalidates_after_crash_before_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            effects: list[dict[str, object]] = []
            lifecycle, policy, task_id = self._completion_ready(root, effects)
            original_transition = lifecycle._append_transition

            def crash_before_verified(session, target, **kwargs):
                if target is TaskState.VERIFIED:
                    raise SimulatedProcessDeath(
                        "completion operation durable, terminal transition not durable"
                    )
                return original_transition(session, target, **kwargs)

            with patch.object(lifecycle, "_append_transition", crash_before_verified):
                with self.assertRaises(SimulatedProcessDeath):
                    lifecycle.complete(task_id, idempotency_key="complete")

            gates = self._completion_gate_events(root, task_id)
            self.assertEqual(len(gates), 1)
            self.assertEqual(gates[0].payload["decision"], "pass")

            fresh = TaskLifecycle(
                root,
                policy=policy,
                effect_runner=lambda action: effects.append(action) or {"status": "ok"},
            )
            fresh.action(
                task_id,
                self._action("changed after the operation"),
                idempotency_key="post-operation-mutation",
            )
            with self.assertRaises(TaskLifecycleError) as stale:
                fresh.complete(task_id, idempotency_key="complete")
            self.assertEqual(stale.exception.code, "completion_snapshot_stale")
            self.assertEqual(fresh.get(task_id).state, TaskState.EXECUTING)

    def test_completion_retry_rejects_direct_workspace_drift(self) -> None:
        for boundary in ("before_operation", "before_transition"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                effects: list[dict[str, object]] = []
                lifecycle, policy, task_id = self._completion_ready(root, effects)
                if boundary == "before_operation":
                    patcher = patch.object(
                        lifecycle,
                        "_append_operation",
                        side_effect=SimulatedProcessDeath("gate durable"),
                    )
                else:
                    original_transition = lifecycle._append_transition

                    def crash_before_verified(session, target, **kwargs):
                        if target is TaskState.VERIFIED:
                            raise SimulatedProcessDeath("operation durable")
                        return original_transition(session, target, **kwargs)

                    patcher = patch.object(
                        lifecycle,
                        "_append_transition",
                        crash_before_verified,
                    )
                with patcher, self.assertRaises(SimulatedProcessDeath):
                    lifecycle.complete(task_id, idempotency_key="complete")

                (root / "external-drift.txt").write_text(
                    "changed outside the ledger",
                    encoding="utf-8",
                )
                fresh = TaskLifecycle(root, policy=policy)
                with self.assertRaises(TaskLifecycleError) as stale:
                    fresh.complete(task_id, idempotency_key="complete")
                self.assertEqual(stale.exception.code, "completion_snapshot_stale")
                self.assertEqual(fresh.get(task_id).state, TaskState.EXECUTING)

    def test_rejection_decision_blocks_effect_after_crash_before_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            effects: list[dict[str, object]] = []
            authorizer = lambda _approver, _stage, _proof: True
            lifecycle = TaskLifecycle(
                root,
                approval_authorizer=authorizer,
                effect_runner=lambda action: effects.append(action) or {"status": "ok"},
            )
            task = lifecycle.begin(
                GoalContract(
                    title="durable rejection",
                    summary="a durable rejection must immediately revoke execution",
                    permissions=PermissionContract(
                        allowed_tools=("file.write",),
                        write_scope=("out",),
                    ),
                ),
                idempotency_key="begin",
            )
            task = lifecycle.action(
                task.task_id,
                self._action("enter executing"),
                idempotency_key="initial-action",
            )
            effects.clear()

            with patch.object(
                lifecycle,
                "_append_transition",
                side_effect=SimulatedProcessDeath(
                    "human rejection durable, terminal transition not durable"
                ),
            ):
                with self.assertRaises(SimulatedProcessDeath):
                    lifecycle.approve(
                        task.task_id,
                        stage="final",
                        approved=False,
                        approver="operator",
                        rationale="the task must stop",
                        idempotency_key="reject",
                        proof="trusted",
                    )

            decisions = [
                event
                for event in EvidenceLedger(
                    root / ".causality" / "ledger.jsonl"
                ).events_for_contract(task.task_id, all_segments=True)
                if event.event_type == "human_decision"
                and event.payload.get("operation") == "approve"
                and event.payload.get("idempotency_key") == "reject"
            ]
            self.assertEqual(len(decisions), 1)
            self.assertIs(decisions[0].payload["approved"], False)

            fresh = TaskLifecycle(
                root,
                approval_authorizer=authorizer,
                effect_runner=lambda action: effects.append(action) or {"status": "ok"},
            )
            with self.assertRaises(TaskLifecycleError):
                fresh.action(
                    task.task_id,
                    self._action("must never execute"),
                    idempotency_key="after-rejection",
                )
            self.assertEqual(effects, [])
            reflected = fresh.reflect(
                task.task_id,
                idempotency_key="reflect-authoritative-rejection",
            )
            self.assertTrue(reflected.terminal)
            self.assertIsNotNone(reflected.reflection)
            count = fresh.ledger.event_count()
            replay = fresh.approve(
                task.task_id,
                stage="final",
                approved=False,
                approver="operator",
                rationale="the task must stop",
                idempotency_key="reject",
                proof="trusted",
            )
            self.assertEqual(replay.reflection, reflected.reflection)
            self.assertEqual(fresh.ledger.event_count(), count)

    def test_legacy_correlated_rejection_is_fail_closed_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle = TaskLifecycle(
                root,
                effect_runner=lambda _action: {"status": "ok"},
            )
            task = lifecycle.begin(
                GoalContract(
                    title="legacy rejection",
                    summary="read an earlier partial-commit order safely",
                    permissions=PermissionContract(
                        allowed_tools=("file.write",),
                        write_scope=("out",),
                    ),
                ),
                idempotency_key="legacy-begin",
            )
            task = lifecycle.action(
                task.task_id,
                self._action("executing"),
                idempotency_key="legacy-action",
            )
            request = {
                "stage": "final",
                "approved": False,
                "approver": "operator",
                "rationale": "legacy rejection",
                "evidence_refs": [],
            }
            digest = canonical_sha256(request)
            operation_id = lifecycle._operation_id(
                task.task_id,
                "approve",
                "legacy-reject",
                digest,
            )
            decision = lifecycle.ledger.append(
                AuditEventType.HUMAN_DECISION,
                {
                    "schema_version": 1,
                    "task_id": task.task_id,
                    "operation_id": operation_id,
                    "idempotency_key": "legacy-reject",
                    "request_sha256": digest,
                    "stage": "final",
                    "approved": False,
                    "approver": "operator",
                    "rationale": "legacy rejection",
                },
                contract_id=task.task_id,
            )
            fresh = TaskLifecycle(root)
            self.assertEqual(fresh.get(task.task_id).state, TaskState.REJECTED)
            with self.assertRaises(TaskLifecycleError):
                fresh.action(
                    task.task_id,
                    self._action("must not run"),
                    idempotency_key="legacy-after-reject",
                )

            transition = lifecycle.ledger.append(
                AuditEventType.STATE_TRANSITION,
                {
                    "schema_version": 1,
                    "task_id": task.task_id,
                    "from_state": "executing",
                    "state": "rejected",
                    "reason": "legacy transition order",
                    "cause_event_hash": decision.entry_hash,
                },
                contract_id=task.task_id,
            )
            lifecycle.ledger.append(
                AuditEventType.TASK_OPERATION,
                {
                    "schema_version": 1,
                    "task_id": task.task_id,
                    "operation": "approve",
                    "operation_id": operation_id,
                    "idempotency_key": "legacy-reject",
                    "request_sha256": digest,
                    "request": request,
                    "response": {
                        "stage": "final",
                        "approved": False,
                        "decision_event_hash": decision.entry_hash,
                    },
                    "outcome": "completed",
                },
                contract_id=task.task_id,
            )
            recovered = fresh.get(task.task_id)
            self.assertEqual(recovered.state, TaskState.REJECTED)
            self.assertEqual(
                recovered.idempotency[("approve", "legacy-reject")].event_hashes,
                (decision.entry_hash, recovered.latest_event_hash),
            )
            self.assertNotEqual(transition.entry_hash, recovered.latest_event_hash)

    def test_partial_recovery_decision_reserves_the_pending_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            effects: list[dict[str, object]] = []

            def uncertain(action):
                effects.append(action)
                raise SimulatedProcessDeath("effect outcome is unknown")

            lifecycle = TaskLifecycle(
                root,
                effect_runner=uncertain,
            )
            task = lifecycle.begin(
                GoalContract(
                    title="recovery reservation",
                    summary="one trusted recovery decision must own the orphan",
                    permissions=PermissionContract(
                        allowed_tools=("file.write",),
                        write_scope=("out",),
                    ),
                ),
                idempotency_key="begin-recovery",
            )
            with self.assertRaises(SimulatedProcessDeath):
                lifecycle.action(
                    task.task_id,
                    self._action("uncertain"),
                    idempotency_key="uncertain-action",
                )

            authorizer = lambda _approver, _stage, _proof: True
            recovery = TaskLifecycle(root, approval_authorizer=authorizer)
            blocked = recovery.get(task.task_id)
            target = blocked.unresolved_intents[0].operation_id
            with patch.object(
                recovery,
                "_append_operation",
                side_effect=SimulatedProcessDeath("decision durable, result lost"),
            ):
                with self.assertRaises(SimulatedProcessDeath):
                    recovery.resolve(
                        task.task_id,
                        operation_id=target,
                        resolution="applied",
                        approver="operator",
                        rationale="the first effect may have applied",
                        idempotency_key="resolve-first",
                        proof="trusted",
                    )

            fresh = TaskLifecycle(root, approval_authorizer=authorizer)
            with self.assertRaises(TaskLifecycleError) as conflict:
                fresh.resolve(
                    task.task_id,
                    operation_id=target,
                    resolution="not_applied",
                    approver="operator",
                    rationale="a contradictory second decision",
                    idempotency_key="resolve-second",
                    proof="trusted",
                )
            self.assertEqual(conflict.exception.code, "recovery_in_progress")
            resumed = fresh.resolve(
                task.task_id,
                operation_id=target,
                resolution="applied",
                approver="operator",
                rationale="the first effect may have applied",
                idempotency_key="resolve-first",
                proof="trusted",
            )
            self.assertEqual(resumed.state, TaskState.BLOCKED)
            self.assertEqual(len(effects), 1)

    def test_partial_completion_escalation_blocks_new_effects(self) -> None:
        for boundary in ("before_operation", "before_transition"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                effects: list[dict[str, object]] = []
                lifecycle, policy, task_id = self._escalation_ready(root, effects)
                if boundary == "before_operation":
                    patcher = patch.object(
                        lifecycle,
                        "_append_operation",
                        side_effect=SimulatedProcessDeath("escalation result lost"),
                    )
                else:
                    original_transition = lifecycle._append_transition

                    def crash_before_blocked(session, target, **kwargs):
                        if target is TaskState.BLOCKED:
                            raise SimulatedProcessDeath("blocked transition lost")
                        return original_transition(session, target, **kwargs)

                    patcher = patch.object(
                        lifecycle,
                        "_append_transition",
                        crash_before_blocked,
                    )
                with patcher, self.assertRaises(SimulatedProcessDeath):
                    lifecycle.complete(task_id, idempotency_key="complete-escalate")

                gate = [
                    event
                    for event in lifecycle.ledger.events_for_contract(
                        task_id, all_segments=True
                    )
                    if event.event_type == "gate_decision"
                    and event.payload.get("idempotency_key") == "complete-escalate"
                ][-1]
                self.assertEqual(gate.payload["decision"], "escalate")
                fresh = TaskLifecycle(
                    root,
                    policy=policy,
                    approval_authorizer=lambda _approver, _stage, _proof: True,
                    effect_runner=lambda action: effects.append(action) or {"status": "ok"},
                )
                blocked = fresh.get(task_id)
                self.assertEqual(blocked.state, TaskState.BLOCKED)
                with self.assertRaises(TaskLifecycleError):
                    fresh.action(
                        task_id,
                        self._action("must not run"),
                        idempotency_key="after-escalation",
                    )
                self.assertEqual(len(effects), 1)
                resumed = fresh.complete(
                    task_id,
                    idempotency_key="complete-escalate",
                )
                self.assertEqual(resumed.state, TaskState.BLOCKED)

    def test_transition_retry_recovers_target_or_api_is_not_public(self) -> None:
        transition = getattr(TaskLifecycle, "transition", None)
        if not callable(transition):
            self.assertFalse(hasattr(TaskLifecycle, "transition"))
            return

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle = TaskLifecycle(root)
            task = lifecycle.begin(
                GoalContract(title="atomic transition", summary=""),
                idempotency_key="begin",
            )
            with patch.object(
                lifecycle,
                "_append_transition",
                side_effect=SimulatedProcessDeath(
                    "transition operation durable, state transition not durable"
                ),
            ):
                with self.assertRaises(SimulatedProcessDeath):
                    lifecycle.transition(
                        task.task_id,
                        TaskState.APPROVED,
                        idempotency_key="approve-transition",
                        reason="exercise the public transition boundary",
                    )

            fresh = TaskLifecycle(root)
            recovered = fresh.transition(
                task.task_id,
                TaskState.APPROVED,
                idempotency_key="approve-transition",
                reason="exercise the public transition boundary",
            )
            self.assertEqual(recovered.state, TaskState.APPROVED)


if __name__ == "__main__":
    unittest.main()
