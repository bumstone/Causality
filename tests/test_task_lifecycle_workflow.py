from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from causality.contracts import (
    AuditEventType,
    EvidenceKind,
    GoalContract,
    PermissionContract,
    VerificationRequirement,
)
from causality.task_lifecycle import (
    TaskLifecycle,
    TaskLifecycleError,
    TaskPolicy,
    TaskState,
    canonical_sha256,
)


COMMAND = (sys.executable, "-c", "raise SystemExit(0)")
REPRODUCE = "root-cause-protocol/reproduce"
HYPOTHESIS = "root-cause-protocol/hypothesis"


class WorkflowLifecycleTests(unittest.TestCase):
    def _lifecycle(self, root: Path) -> TaskLifecycle:
        return TaskLifecycle(
            root,
            policy=TaskPolicy(verification_commands=(COMMAND,)),
            effect_runner=lambda _action: {"status": "ok"},
        )

    @staticmethod
    def _contract(
        title: str = "debug checkout",
        *,
        max_failed_hypotheses: int = 3,
    ) -> GoalContract:
        return GoalContract(
            title=title,
            summary="prove the workflow loop",
            permissions=PermissionContract(
                allowed_tools=("file.write", "shell"),
                write_scope=("out",),
            ),
            verification_requirements=(
                VerificationRequirement(id="unit", argv=COMMAND),
            ),
            stopping_policy={
                "max_iterations": 8,
                "max_failed_hypotheses": max_failed_hypotheses,
                "no_progress_iterations": 2,
            },
        )

    def _begin(self, root: Path, *, key: str = "workflow-begin"):
        lifecycle = self._lifecycle(root)
        task = lifecycle.begin(
            self._contract(),
            idempotency_key=key,
            workflow="root-cause-protocol",
        )
        return lifecycle, task

    def _phase_evidence(self, lifecycle: TaskLifecycle, task_id: str, suffix: str):
        session = lifecycle.action(
            task_id,
            {"kind": "file_write", "path": "out/result.txt", "content": suffix},
            idempotency_key=f"action-{suffix}",
        )
        action_hash = session.idempotency[("action", f"action-{suffix}")].event_hashes[-1]
        session = lifecycle.verify(task_id, "unit", idempotency_key=f"verify-{suffix}")
        verify_hash = session.idempotency[("verify", f"verify-{suffix}")].response[
            "event_hash"
        ]
        verdict_hashes = []
        for index, verifier in enumerate(("correctness", "evidence"), start=1):
            key = f"verdict-{suffix}-{index}"
            session = lifecycle.verdict(
                task_id,
                verifier=verifier,
                status="pass",
                rationale=f"{verifier} approved phase {suffix}",
                evidence_refs=(verify_hash,),
                idempotency_key=key,
            )
            verdict_hashes.append(
                session.idempotency[("verdict", key)].response["decision_event_hash"]
            )
        return (action_hash, verify_hash, *verdict_hashes)

    def _start_hypothesis(
        self,
        lifecycle: TaskLifecycle,
        task,
        suffix: str,
    ):
        task = lifecycle.phase(
            task.task_id,
            phase_id=REPRODUCE,
            action="start",
            idempotency_key=f"{suffix}-reproduce-start",
        )
        refs = self._phase_evidence(lifecycle, task.task_id, f"{suffix}-reproduce")
        task = lifecycle.phase(
            task.task_id,
            phase_id=REPRODUCE,
            action="finish",
            status="passed",
            evidence_refs=refs,
            idempotency_key=f"{suffix}-reproduce-finish",
        )
        return lifecycle.phase(
            task.task_id,
            phase_id=HYPOTHESIS,
            action="start",
            idempotency_key=f"{suffix}-hypothesis-start",
        )

    @staticmethod
    def _hypothesis_evidence(
        lifecycle: TaskLifecycle,
        task_id: str,
        suffix: str,
    ) -> str:
        task = lifecycle.action(
            task_id,
            {
                "kind": "file_write",
                "path": "out/hypothesis.txt",
                "content": suffix,
            },
            idempotency_key=f"hypothesis-action-{suffix}",
        )
        return task.idempotency[
            ("action", f"hypothesis-action-{suffix}")
        ].event_hashes[-1]

    @staticmethod
    def _record_hypothesis(
        lifecycle: TaskLifecycle,
        task_id: str,
        *,
        suffix: str,
        status: str,
        evidence_ref: str,
    ):
        return lifecycle.hypothesis(
            task_id,
            phase_id=HYPOTHESIS,
            hypothesis=f"candidate cause {suffix}",
            verifier=f"debugger-{suffix}",
            status=status,
            rationale=f"experiment {suffix} was {status}",
            evidence_refs=(evidence_ref,),
            idempotency_key=f"hypothesis-{suffix}",
        )

    def test_explicit_workflow_snapshot_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _lifecycle, task = self._begin(root)

            self.assertEqual(task.workflow, "root-cause-protocol")
            self.assertEqual(
                [phase.phase_id for phase in task.workflow_phases],
                [
                    "root-cause-protocol/reproduce",
                    "root-cause-protocol/hypothesis",
                    "root-cause-protocol/verify",
                    "root-cause-protocol/fix",
                ],
            )
            self.assertEqual(task.current_phase_id, "root-cause-protocol/reproduce")
            self.assertTrue(all(phase.status == "pending" for phase in task.workflow_phases))
            self.assertEqual(
                task.allowed_next,
                ("approve", "phase_start", "reject"),
            )
            with patch(
                "causality.task_lifecycle._workflow_snapshot",
                side_effect=AssertionError("persisted tasks must not re-resolve playbooks"),
            ):
                restarted = self._lifecycle(root)
                self.assertEqual(
                    restarted.get(task.task_id).to_dict(),
                    task.to_dict(),
                )
                self.assertEqual(
                    restarted.begin(
                        self._contract(),
                        idempotency_key="workflow-begin",
                        workflow="root-cause-protocol",
                    ),
                    task,
                )

    def test_begin_workflow_is_idempotent_and_conflicts_with_other_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle, task = self._begin(root)

            replay = lifecycle.begin(
                self._contract(),
                idempotency_key="workflow-begin",
                workflow="root-cause-protocol",
            )
            self.assertEqual(replay, task)
            with self.assertRaises(TaskLifecycleError) as caught:
                lifecycle.begin(
                    self._contract(),
                    idempotency_key="workflow-begin",
                    workflow="auto",
                )
            self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_projection_rejects_empty_explicit_workflow_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle = self._lifecycle(root)
            contract = self._contract()
            key = "forged-explicit"
            task_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"causality:{str(root.resolve()).casefold()}:{key}",
                )
            )
            bound = lifecycle.runtime.create_contract(
                lifecycle._effective_contract(contract, task_id)
            )
            request = contract.to_dict()
            for name in (
                "goal_id",
                "created_at",
                "workspace_root",
                "state",
                "approval_required",
            ):
                request.pop(name, None)
            request.update({"workflow": "root-cause-protocol", "phase_plan": []})
            lifecycle.ledger.append(
                AuditEventType.TASK_STARTED,
                {
                    "schema_version": 1,
                    "task_id": task_id,
                    "idempotency_key": key,
                    "request_sha256": canonical_sha256(request),
                    "request": request,
                    "workflow": "root-cause-protocol",
                    "phase_plan": [],
                    "response": {
                        "task_id": task_id,
                        "contract_id": task_id,
                        "workflow": "root-cause-protocol",
                        "phase_plan": [],
                    },
                },
                contract_id=bound.goal_id,
            )

            with self.assertRaises(TaskLifecycleError) as caught:
                lifecycle.get(task_id)

            self.assertEqual(caught.exception.code, "invalid_task_event")

    def test_phase_pass_requires_current_evidence_and_two_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle, task = self._begin(root)
            task = lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="start",
                idempotency_key="phase-start",
            )
            self.assertEqual(task.state, TaskState.EXECUTING)
            self.assertEqual(task.workflow_phases[0].status, "running")
            self.assertIn("phase_finish", task.allowed_next)
            self.assertNotIn("complete", task.allowed_next)

            before = lifecycle.ledger.event_count()
            with self.assertRaises(TaskLifecycleError) as caught:
                lifecycle.phase(
                    task.task_id,
                    phase_id=REPRODUCE,
                    action="finish",
                    status="passed",
                    evidence_refs=(),
                    idempotency_key="phase-empty",
                )
            self.assertEqual(caught.exception.code, "phase_evidence_incomplete")
            self.assertEqual(lifecycle.ledger.event_count(), before)

            refs = self._phase_evidence(lifecycle, task.task_id, "first")
            with self.assertRaises(TaskLifecycleError) as one_verdict:
                lifecycle.phase(
                    task.task_id,
                    phase_id=REPRODUCE,
                    action="finish",
                    status="passed",
                    evidence_refs=refs[:-1],
                    idempotency_key="phase-one-verdict",
                )
            self.assertEqual(one_verdict.exception.code, "phase_evidence_incomplete")

            task = lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="finish",
                status="passed",
                evidence_refs=refs,
                idempotency_key="phase-pass",
            )
            self.assertEqual(task.workflow_phases[0].status, "passed")
            self.assertEqual(task.workflow_phases[0].evidence_hashes, refs)
            self.assertEqual(task.current_phase_id, "root-cause-protocol/hypothesis")
            self.assertEqual(task.allowed_next, ("phase_start", "reject"))
            self.assertEqual(self._lifecycle(root).get(task.task_id), task)

            before = lifecycle.ledger.event_count()
            with self.assertRaises(TaskLifecycleError) as stale:
                lifecycle.phase(
                    task.task_id,
                    phase_id=REPRODUCE,
                    action="start",
                    idempotency_key="stale-client",
                )
            self.assertEqual(stale.exception.code, "phase_mismatch")
            self.assertEqual(lifecycle.ledger.event_count(), before)

    def test_verification_result_cannot_replace_required_phase_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle, task = self._begin(root)
            task = lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="start",
                idempotency_key="verify-only-start",
            )
            task = lifecycle.verify(
                task.task_id,
                "unit",
                idempotency_key="verify-only-run",
            )
            verify_record = task.idempotency[("verify", "verify-only-run")]
            verify_hash = verify_record.response["event_hash"]
            verify_action_hash = next(
                event.entry_hash
                for event in lifecycle.ledger.events_for_contract(
                    task.task_id, all_segments=True
                )
                if event.event_type == AuditEventType.TASK_ACTION_RESULT.value
                and event.payload.get("operation") == "verify"
            )
            verdict_hashes = []
            for index, verifier in enumerate(("correctness", "evidence"), start=1):
                key = f"verify-only-verdict-{index}"
                task = lifecycle.verdict(
                    task.task_id,
                    verifier=verifier,
                    status="pass",
                    rationale="verification passed but no phase action ran",
                    evidence_refs=(verify_hash,),
                    idempotency_key=key,
                )
                verdict_hashes.append(
                    task.idempotency[("verdict", key)].response[
                        "decision_event_hash"
                    ]
                )

            with self.assertRaises(TaskLifecycleError) as caught:
                lifecycle.phase(
                    task.task_id,
                    phase_id=REPRODUCE,
                    action="finish",
                    status="passed",
                    evidence_refs=(verify_action_hash, verify_hash, *verdict_hashes),
                    idempotency_key="verify-only-finish",
                )

            self.assertEqual(caught.exception.code, "phase_evidence_incomplete")

    def test_manual_verification_can_satisfy_phase_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle = TaskLifecycle(
                root,
                effect_runner=lambda _action: {"status": "ok"},
                approval_authorizer=lambda _who, _stage, proof: proof == "trusted",
            )
            task = lifecycle.begin(
                GoalContract(
                    title="manual phase verification",
                    summary="accept a human verification decision as phase evidence",
                    permissions=PermissionContract(
                        allowed_tools=("file.write",),
                        write_scope=("out",),
                    ),
                    verification_requirements=(
                        VerificationRequirement(
                            id="visual",
                            argv=(),
                            manual=True,
                        ),
                    ),
                ),
                idempotency_key="manual-phase-begin",
                workflow="root-cause-protocol",
            )
            task = lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="start",
                idempotency_key="manual-phase-start",
            )
            task = lifecycle.action(
                task.task_id,
                {"kind": "file_write", "path": "out/result.txt", "content": "ok"},
                idempotency_key="manual-phase-action",
            )
            action_hash = task.idempotency[
                ("action", "manual-phase-action")
            ].event_hashes[-1]
            evidence = lifecycle.runtime.record_evidence(
                lifecycle._contract(task),
                EvidenceKind.A11Y_REPORT,
                {"summary": "reviewed current visual state"},
            )
            task = lifecycle.verify(
                task.task_id,
                "visual",
                idempotency_key="manual-phase-verify",
                mode="manual",
                evidence_hash=evidence.entry_hash,
                approved=True,
                approver="reviewer",
                rationale="visual state matches",
                proof="trusted",
            )
            manual_record = task.idempotency[("verify", "manual-phase-verify")]
            manual_operation_hash = manual_record.event_hashes[-1]
            manual_event = next(
                event
                for event in lifecycle.ledger.events_for_contract(
                    task.task_id, all_segments=True
                )
                if event.entry_hash == manual_operation_hash
            )
            self.assertEqual(manual_event.event_type, AuditEventType.TASK_OPERATION.value)

            verdict_hashes = []
            for index, verifier in enumerate(("correctness", "evidence"), start=1):
                key = f"manual-phase-verdict-{index}"
                task = lifecycle.verdict(
                    task.task_id,
                    verifier=verifier,
                    status="pass",
                    rationale="manual verification is current and approved",
                    evidence_refs=(evidence.entry_hash,),
                    idempotency_key=key,
                )
                verdict_hashes.append(
                    task.idempotency[("verdict", key)].response[
                        "decision_event_hash"
                    ]
                )

            task = lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="finish",
                status="passed",
                evidence_refs=(
                    action_hash,
                    evidence.entry_hash,
                    manual_operation_hash,
                    *verdict_hashes,
                ),
                idempotency_key="manual-phase-finish",
            )

            self.assertEqual(task.workflow_phases[0].status, "passed")

    def test_projection_revalidates_phase_evidence_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle, task = self._begin(root)
            task = lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="start",
                idempotency_key="forged-start",
            )
            task = lifecycle.action(
                task.task_id,
                {"kind": "file_write", "path": "out/result.txt", "content": "x"},
                idempotency_key="forged-action",
            )
            action_hash = task.idempotency[("action", "forged-action")].event_hashes[-1]
            request = {
                "action": "finish",
                "phase_id": REPRODUCE,
                "status": "passed",
                "evidence_refs": [action_hash],
            }
            lifecycle._append_operation(
                task,
                "phase",
                "forged-finish",
                canonical_sha256(request),
                request,
                {
                    "phase": {
                        "phase_id": REPRODUCE,
                        "from_status": "running",
                        "status": "passed",
                        "attempt": 1,
                        "evidence_hashes": [action_hash],
                    }
                },
            )

            with self.assertRaises(TaskLifecycleError) as caught:
                lifecycle.get(task.task_id)

            self.assertEqual(caught.exception.code, "phase_evidence_incomplete")

    def test_phase_verification_must_follow_last_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle, task = self._begin(root)
            for index, phase in enumerate(task.workflow_phases[:3], start=1):
                task = lifecycle.phase(
                    task.task_id,
                    phase_id=phase.phase_id,
                    action="start",
                    idempotency_key=f"freshness-prior-start-{index}",
                )
                refs = self._phase_evidence(
                    lifecycle,
                    task.task_id,
                    f"freshness-prior-{index}",
                )
                task = lifecycle.phase(
                    task.task_id,
                    phase_id=phase.phase_id,
                    action="finish",
                    status="passed",
                    evidence_refs=refs,
                    idempotency_key=f"freshness-prior-finish-{index}",
                )
            fix_phase = task.current_phase_id
            self.assertEqual(fix_phase, "root-cause-protocol/fix")
            task = lifecycle.phase(
                task.task_id,
                phase_id=fix_phase,
                action="start",
                idempotency_key="freshness-start",
            )
            task = lifecycle.action(
                task.task_id,
                {"kind": "file_write", "path": "out/result.txt", "content": "first"},
                idempotency_key="freshness-first-action",
            )
            first_action_hash = task.idempotency[
                ("action", "freshness-first-action")
            ].event_hashes[-1]
            task = lifecycle.verify(
                task.task_id,
                "unit",
                idempotency_key="freshness-verify",
            )
            verify_hash = task.idempotency[("verify", "freshness-verify")].response[
                "event_hash"
            ]
            verdict_hashes = []
            for index, verifier in enumerate(("correctness", "evidence"), start=1):
                key = f"freshness-verdict-{index}"
                task = lifecycle.verdict(
                    task.task_id,
                    verifier=verifier,
                    status="pass",
                    rationale="verified before mutation",
                    evidence_refs=(verify_hash,),
                    idempotency_key=key,
                )
                verdict_hashes.append(
                    task.idempotency[("verdict", key)].response["decision_event_hash"]
                )
            task = lifecycle.action(
                task.task_id,
                {"kind": "file_write", "path": "out/result.txt", "content": "late"},
                idempotency_key="freshness-uncited-action",
            )

            with self.assertRaises(TaskLifecycleError) as caught:
                lifecycle.phase(
                    task.task_id,
                    phase_id=fix_phase,
                    action="finish",
                    status="passed",
                    evidence_refs=(first_action_hash, verify_hash, *verdict_hashes),
                    idempotency_key="stale-verification",
                )

            self.assertEqual(caught.exception.code, "phase_evidence_incomplete")

            task = lifecycle.verify(
                task.task_id,
                "unit",
                idempotency_key="freshness-new-verification",
            )
            new_verify_hash = task.idempotency[
                ("verify", "freshness-new-verification")
            ].response["event_hash"]
            with self.assertRaises(TaskLifecycleError) as stale_verdicts:
                lifecycle.phase(
                    task.task_id,
                    phase_id=fix_phase,
                    action="finish",
                    status="passed",
                    evidence_refs=(
                        first_action_hash,
                        verify_hash,
                        *verdict_hashes,
                        new_verify_hash,
                    ),
                    idempotency_key="stale-verdicts",
                )

            self.assertEqual(
                stale_verdicts.exception.code,
                "phase_evidence_incomplete",
            )

    def test_completed_workflow_allows_global_completion_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle, task = self._begin(root)
            for index, phase in enumerate(task.workflow_phases, start=1):
                task = lifecycle.phase(
                    task.task_id,
                    phase_id=phase.phase_id,
                    action="start",
                    idempotency_key=f"repair-start-{index}",
                )
                refs = self._phase_evidence(lifecycle, task.task_id, f"repair-{index}")
                task = lifecycle.phase(
                    task.task_id,
                    phase_id=phase.phase_id,
                    action="finish",
                    status="passed",
                    evidence_refs=refs,
                    idempotency_key=f"repair-finish-{index}",
                )

            self.assertIsNone(task.current_phase_id)
            self.assertEqual(
                task.allowed_next,
                ("verify", "verdict", "append_evidence", "complete"),
            )
            repaired = lifecycle.verify(
                task.task_id,
                "unit",
                idempotency_key="global-repair-verification",
            )
            self.assertEqual(repaired.current_phase_id, None)
            self.assertIn(("verify", "global-repair-verification"), repaired.idempotency)

    def test_retry_rejects_evidence_from_previous_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle, task = self._begin(root)
            task = lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="start",
                idempotency_key="start-one",
            )
            stale_refs = self._phase_evidence(lifecycle, task.task_id, "stale")
            task = lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="finish",
                status="failed",
                evidence_refs=stale_refs,
                idempotency_key="fail-one",
            )
            self.assertEqual(task.workflow_phases[0].status, "failed")
            task = lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="start",
                idempotency_key="start-two",
            )
            self.assertEqual(task.workflow_phases[0].attempt, 2)

            with self.assertRaises(TaskLifecycleError) as caught:
                lifecycle.phase(
                    task.task_id,
                    phase_id=REPRODUCE,
                    action="finish",
                    status="passed",
                    evidence_refs=stale_refs,
                    idempotency_key="stale-pass",
                )
            self.assertEqual(caught.exception.code, "phase_evidence_stale")

    def test_phase_operation_replays_and_conflicting_request_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle, task = self._begin(root)
            first = lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="start",
                idempotency_key="same-phase-key",
            )
            count = lifecycle.ledger.event_count()

            replay = lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="start",
                idempotency_key="same-phase-key",
            )
            self.assertEqual(replay, first)
            self.assertEqual(lifecycle.ledger.event_count(), count)
            with self.assertRaises(TaskLifecycleError) as caught:
                lifecycle.phase(
                    task.task_id,
                    phase_id=REPRODUCE,
                    action="finish",
                    status="failed",
                    idempotency_key="same-phase-key",
                )
            self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_phase_block_recovery_rejects_historical_finish_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def build_lifecycle() -> TaskLifecycle:
                return TaskLifecycle(
                    root,
                    policy=TaskPolicy(verification_commands=(COMMAND,)),
                    effect_runner=lambda _action: {"status": "ok"},
                    approval_authorizer=lambda *_args: True,
                )

            lifecycle = build_lifecycle()
            task = lifecycle.begin(
                self._contract(),
                idempotency_key="phase-history-begin",
                workflow="root-cause-protocol",
            )
            lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="start",
                idempotency_key="phase-history-start-1",
            )
            old_refs = self._phase_evidence(lifecycle, task.task_id, "phase-old")
            old_block = lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="finish",
                status="blocked",
                evidence_refs=old_refs,
                idempotency_key="phase-block-old",
            )
            lifecycle.approve(
                task.task_id,
                stage="phase",
                phase_id=REPRODUCE,
                approved=True,
                approver="operator",
                rationale="reviewed the first phase block",
                evidence_refs=old_block.approval_evidence_refs,
                idempotency_key="phase-block-old-approval",
                proof="trusted",
            )
            lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="start",
                idempotency_key="phase-history-start-2",
            )
            current_refs = self._phase_evidence(
                lifecycle,
                task.task_id,
                "phase-current",
            )
            original_transition = lifecycle._append_transition

            def crash_before_block(session, target, **kwargs):
                if target is TaskState.BLOCKED:
                    raise RuntimeError("phase block transition lost")
                return original_transition(session, target, **kwargs)

            with patch.object(
                lifecycle,
                "_append_transition",
                side_effect=crash_before_block,
            ), self.assertRaisesRegex(RuntimeError, "transition lost"):
                lifecycle.phase(
                    task.task_id,
                    phase_id=REPRODUCE,
                    action="finish",
                    status="blocked",
                    evidence_refs=current_refs,
                    idempotency_key="phase-block-current",
                )

            recovered = build_lifecycle()
            pending = recovered.get(task.task_id)
            self.assertEqual(pending.allowed_next, ("phase_finish", "reject"))
            self.assertEqual(
                pending.to_dict()["recommended_next"]["operation"],
                "phase_finish",
            )
            self.assertTrue(
                pending.to_dict()["recommended_next"]["replay_required"]
            )
            before = recovered.ledger.event_count()
            historical = recovered.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="finish",
                status="blocked",
                evidence_refs=old_refs,
                idempotency_key="phase-block-old",
            )
            self.assertEqual(historical, pending)
            self.assertEqual(recovered.ledger.event_count(), before)
            blocked = recovered.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="finish",
                status="blocked",
                evidence_refs=current_refs,
                idempotency_key="phase-block-current",
            )
            self.assertEqual(blocked.state, TaskState.BLOCKED)
            self.assertEqual(blocked.allowed_next, ("approve",))
            self.assertEqual(
                blocked.to_dict()["recommended_next"]["evidence_refs"],
                list(blocked.approval_evidence_refs),
            )
            self.assertEqual(recovered.ledger.event_count(), before + 1)

    def test_phase_enabled_task_blocks_effect_before_phase_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            calls = []
            lifecycle = TaskLifecycle(
                root,
                policy=TaskPolicy(verification_commands=(COMMAND,)),
                effect_runner=lambda action: calls.append(action) or {"status": "ok"},
            )
            task = lifecycle.begin(
                self._contract(),
                idempotency_key="blocked-before-phase",
                workflow="root-cause-protocol",
            )
            before = lifecycle.ledger.event_count()

            for phase_id, action, code in (
                (HYPOTHESIS, "start", "phase_mismatch"),
                (REPRODUCE, "finish", "phase_not_running"),
            ):
                with self.assertRaises(TaskLifecycleError) as invalid:
                    lifecycle.phase(
                        task.task_id,
                        phase_id=phase_id,
                        action=action,
                        status="blocked" if action == "finish" else None,
                        evidence_refs=(),
                        idempotency_key=f"invalid-first-{action}",
                    )
                self.assertEqual(invalid.exception.code, code)
                self.assertEqual(lifecycle.ledger.event_count(), before)
                self.assertEqual(lifecycle.get(task.task_id).state, TaskState.PLANNED)

            with self.assertRaises(TaskLifecycleError) as caught:
                lifecycle.action(
                    task.task_id,
                    {"kind": "file_write", "path": "out/result.txt", "content": "x"},
                    idempotency_key="early-effect",
                )

            self.assertEqual(caught.exception.code, "phase_not_running")
            self.assertEqual(calls, [])
            self.assertEqual(lifecycle.ledger.event_count(), before)

    def test_blocking_verification_requires_exact_replay_before_phase_approval(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            command = (sys.executable, "-c", "import time; time.sleep(1)")

            def build_lifecycle() -> TaskLifecycle:
                return TaskLifecycle(
                    root,
                    policy=TaskPolicy(verification_commands=(command,)),
                    approval_authorizer=lambda *_args: True,
                )

            lifecycle = build_lifecycle()
            task = lifecycle.begin(
                GoalContract(
                    title="recover timed out workflow verification",
                    summary="block the phase until exact replay and review",
                    permissions=PermissionContract(allowed_tools=("shell",)),
                    verification_requirements=(
                        VerificationRequirement(
                            id="timeout",
                            argv=command,
                            timeout_seconds=0.05,
                        ),
                    ),
                ),
                idempotency_key="verify-block-begin",
                workflow="root-cause-protocol",
            )
            lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="start",
                idempotency_key="verify-block-phase",
            )
            first_block = lifecycle.verify(
                task.task_id,
                "timeout",
                idempotency_key="verify-block-old",
            )
            lifecycle.approve(
                task.task_id,
                stage="phase",
                phase_id=REPRODUCE,
                approved=True,
                approver="operator",
                rationale="reviewed the first timed out verification",
                evidence_refs=first_block.approval_evidence_refs,
                idempotency_key="verify-old-approval",
                proof="trusted",
            )
            lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="start",
                idempotency_key="verify-block-phase-2",
            )
            original_transition = lifecycle._append_transition

            def crash_before_block(session, target, **kwargs):
                if target is TaskState.BLOCKED:
                    raise RuntimeError("verification result durable, transition lost")
                return original_transition(session, target, **kwargs)

            with patch.object(
                lifecycle,
                "_append_transition",
                side_effect=crash_before_block,
            ), self.assertRaisesRegex(RuntimeError, "transition lost"):
                lifecycle.verify(
                    task.task_id,
                    "timeout",
                    idempotency_key="verify-block",
                )

            recovered = build_lifecycle()
            pending = recovered.get(task.task_id)
            self.assertEqual(pending.state, TaskState.BLOCKED)
            self.assertEqual(pending.workflow_phases[0].status, "blocked")
            self.assertEqual(pending.allowed_next, ("verify", "reject"))
            self.assertEqual(
                pending.to_dict()["recommended_next"]["operation"], "verify"
            )
            self.assertTrue(
                pending.to_dict()["recommended_next"]["replay_required"]
            )
            self.assertEqual(
                pending.blocked_reason,
                "workflow recovery requires an exact operation replay",
            )
            before = recovered.ledger.event_count()
            historical = recovered.verify(
                task.task_id,
                "timeout",
                idempotency_key="verify-block-old",
            )
            self.assertEqual(historical, pending)
            self.assertEqual(recovered.ledger.event_count(), before)
            with self.assertRaises(TaskLifecycleError) as wrong_replay:
                recovered.verify(
                    task.task_id,
                    "timeout",
                    idempotency_key="different-verify-key",
                )
            self.assertEqual(wrong_replay.exception.code, "phase_not_running")
            self.assertEqual(recovered.ledger.event_count(), before)
            with self.assertRaises(TaskLifecycleError) as premature:
                recovered.approve(
                    task.task_id,
                    stage="phase",
                    phase_id=REPRODUCE,
                    approved=True,
                    approver="operator",
                    rationale="must replay the interrupted verification first",
                    evidence_refs=pending.approval_evidence_refs,
                    idempotency_key="premature-verify-approval",
                    proof="trusted",
                )
            self.assertEqual(premature.exception.code, "recovery_required")
            self.assertEqual(recovered.ledger.event_count(), before)

            blocked = recovered.verify(
                task.task_id,
                "timeout",
                idempotency_key="verify-block",
            )
            self.assertEqual(blocked.state, TaskState.BLOCKED)
            self.assertEqual(blocked.workflow_phases[0].status, "blocked")
            self.assertEqual(blocked.allowed_next, ("approve",))
            self.assertEqual(len(blocked.approval_evidence_refs), 1)
            approved = recovered.approve(
                task.task_id,
                stage="phase",
                phase_id=REPRODUCE,
                approved=True,
                approver="operator",
                rationale="reviewed the timed out verification",
                evidence_refs=blocked.approval_evidence_refs,
                idempotency_key="verify-phase-approval",
                proof="trusted",
            )
            self.assertEqual(approved.state, TaskState.EXECUTING)
            self.assertEqual(approved.workflow_phases[0].status, "failed")
            self.assertEqual(approved.allowed_next, ("phase_start", "reject"))

    def test_mutating_verification_blocks_the_running_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            command = (
                sys.executable,
                "-c",
                "from pathlib import Path; Path('unexpected.txt').write_text('x')",
            )
            lifecycle = TaskLifecycle(
                root,
                policy=TaskPolicy(verification_commands=(command,)),
            )
            task = lifecycle.begin(
                GoalContract(
                    title="detect mutating verification",
                    summary="a passing check must still be read-only",
                    permissions=PermissionContract(allowed_tools=("shell",)),
                    verification_requirements=(
                        VerificationRequirement(id="mutating", argv=command),
                    ),
                ),
                idempotency_key="mutating-verify-begin",
                workflow="root-cause-protocol",
            )
            lifecycle.phase(
                task.task_id,
                phase_id=REPRODUCE,
                action="start",
                idempotency_key="mutating-verify-phase",
            )

            blocked = lifecycle.verify(
                task.task_id,
                "mutating",
                idempotency_key="mutating-verify",
            )

            self.assertEqual(blocked.state, TaskState.BLOCKED)
            self.assertEqual(blocked.workflow_phases[0].status, "blocked")
            self.assertEqual(blocked.allowed_next, ("approve",))
            self.assertEqual(len(blocked.approval_evidence_refs), 1)
            self.assertEqual(
                blocked.requirement_results["mutating"]["status"],
                "fail",
            )

    def test_hypothesis_outcomes_count_only_rejections_and_replay_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle, task = self._begin(root)
            task = self._start_hypothesis(lifecycle, task, "outcomes")
            self.assertIn("hypothesis", task.allowed_next)

            for suffix, status, expected in (
                ("supported", "supported", 0),
                ("unclear", "inconclusive", 0),
                ("rejected", "rejected", 1),
            ):
                evidence = self._hypothesis_evidence(
                    lifecycle,
                    task.task_id,
                    suffix,
                )
                task = self._record_hypothesis(
                    lifecycle,
                    task.task_id,
                    suffix=suffix,
                    status=status,
                    evidence_ref=evidence,
                )
                self.assertEqual(task.hypothesis_count, expected)

            count = lifecycle.ledger.event_count()
            replay = self._record_hypothesis(
                lifecycle,
                task.task_id,
                suffix="rejected",
                status="rejected",
                evidence_ref=self._hypothesis_evidence(
                    lifecycle,
                    task.task_id,
                    "rejected",
                ),
            )
            self.assertEqual(replay.hypothesis_count, 1)
            self.assertEqual(lifecycle.ledger.event_count(), count)
            self.assertEqual(self._lifecycle(root).get(task.task_id), replay)

    def test_third_rejection_blocks_effects_until_trusted_phase_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            calls = []
            lifecycle = TaskLifecycle(
                root,
                policy=TaskPolicy(verification_commands=(COMMAND,)),
                approval_authorizer=lambda _approver, stage, proof: (
                    stage in {"phase", "final"} and proof == "trusted"
                ),
                effect_runner=lambda action: calls.append(action) or {"status": "ok"},
            )
            task = lifecycle.begin(
                self._contract(),
                idempotency_key="threshold-begin",
                workflow="root-cause-protocol",
            )
            task = self._start_hypothesis(lifecycle, task, "threshold")
            hypothesis_hashes = []
            for index in range(1, 4):
                suffix = f"threshold-{index}"
                evidence = self._hypothesis_evidence(
                    lifecycle,
                    task.task_id,
                    suffix,
                )
                task = self._record_hypothesis(
                    lifecycle,
                    task.task_id,
                    suffix=suffix,
                    status="rejected",
                    evidence_ref=evidence,
                )
                hypothesis_hashes.append(
                    task.idempotency[("hypothesis", f"hypothesis-{suffix}")]
                    .event_hashes[-1]
                )

            self.assertEqual(task.hypothesis_count, 3)
            self.assertEqual(task.state, TaskState.BLOCKED)
            self.assertEqual(task.workflow_phases[1].status, "blocked")
            self.assertEqual(task.allowed_next, ("approve",))
            gate_events = [
                event
                for event in lifecycle.ledger.events_for_contract(
                    task.task_id,
                    all_segments=True,
                )
                if event.event_type == "gate_decision"
                and event.payload.get("operation") == "hypothesis"
            ]
            self.assertEqual(len(gate_events), 1)
            self.assertEqual(gate_events[0].payload["decision"], "escalate")
            approval_refs = (*hypothesis_hashes, gate_events[0].entry_hash)
            self.assertEqual(task.approval_evidence_refs, approval_refs)
            self.assertEqual(
                task.to_dict()["approval_evidence_refs"], list(approval_refs)
            )

            before_calls = len(calls)
            with self.assertRaises(TaskLifecycleError):
                lifecycle.action(
                    task.task_id,
                    {
                        "kind": "file_write",
                        "path": "out/blocked.txt",
                        "content": "must not run",
                    },
                    idempotency_key="blocked-effect",
                )
            self.assertEqual(len(calls), before_calls)

            before = lifecycle.ledger.event_count()
            with self.assertRaises(TaskLifecycleError) as unrelated:
                lifecycle.approve(
                    task.task_id,
                    stage="phase",
                    phase_id=HYPOTHESIS,
                    approved=True,
                    approver="operator",
                    rationale="reviewed rejected hypotheses",
                    evidence_refs=(task.event_hashes[0],),
                    idempotency_key="phase-approval-unrelated",
                    proof="trusted",
                )
            self.assertEqual(unrelated.exception.code, "evidence_scope_mismatch")
            self.assertEqual(lifecycle.ledger.event_count(), before)

            with self.assertRaises(TaskLifecycleError) as untrusted:
                lifecycle.approve(
                    task.task_id,
                    stage="phase",
                    phase_id=HYPOTHESIS,
                    approved=True,
                    approver="operator",
                    rationale="reviewed rejected hypotheses",
                    evidence_refs=approval_refs,
                    idempotency_key="phase-approval",
                    proof="untrusted",
                )
            self.assertEqual(untrusted.exception.code, "approval_required")
            self.assertEqual(lifecycle.ledger.event_count(), before)

            task = lifecycle.approve(
                task.task_id,
                stage="phase",
                phase_id=HYPOTHESIS,
                approved=True,
                approver="operator",
                rationale="reviewed rejected hypotheses",
                evidence_refs=approval_refs,
                idempotency_key="phase-approval",
                proof="trusted",
            )
            self.assertEqual(task.state, TaskState.EXECUTING)
            self.assertEqual(task.workflow_phases[1].status, "failed")
            self.assertEqual(task.hypothesis_count, 3)
            self.assertEqual(task.allowed_next, ("phase_start", "reject"))

            task = lifecycle.phase(
                task.task_id,
                phase_id=HYPOTHESIS,
                action="start",
                idempotency_key="hypothesis-restart",
            )
            self.assertEqual(task.workflow_phases[1].attempt, 2)
            evidence = self._hypothesis_evidence(
                lifecycle,
                task.task_id,
                "after-approval",
            )
            task = self._record_hypothesis(
                lifecycle,
                task.task_id,
                suffix="after-approval",
                status="rejected",
                evidence_ref=evidence,
            )
            self.assertEqual(task.hypothesis_count, 4)
            self.assertEqual(task.state, TaskState.EXECUTING)
            self.assertEqual(
                task.idempotency[("hypothesis", "hypothesis-after-approval")]
                .response["failed_hypotheses"],
                1,
            )
            for index in (2, 3):
                suffix = f"after-approval-{index}"
                evidence = self._hypothesis_evidence(
                    lifecycle,
                    task.task_id,
                    suffix,
                )
                task = self._record_hypothesis(
                    lifecycle,
                    task.task_id,
                    suffix=suffix,
                    status="rejected",
                    evidence_ref=evidence,
                )
            self.assertEqual(task.state, TaskState.BLOCKED)
            self.assertEqual(task.hypothesis_count, 6)

            before = lifecycle.ledger.event_count()
            replay = lifecycle.approve(
                task.task_id,
                stage="phase",
                phase_id=HYPOTHESIS,
                approved=True,
                approver="operator",
                rationale="reviewed rejected hypotheses",
                evidence_refs=approval_refs,
                idempotency_key="phase-approval",
                proof="trusted",
            )
            self.assertEqual(replay.state, TaskState.BLOCKED)
            self.assertEqual(replay.workflow_phases[1].status, "blocked")
            self.assertEqual(lifecycle.ledger.event_count(), before)

            terminal = lifecycle.approve(
                task.task_id,
                stage="final",
                approved=False,
                approver="operator",
                rationale="stop the bounded debug task",
                evidence_refs=(),
                idempotency_key="final-rejection",
                proof="trusted",
            )
            self.assertEqual(terminal.state, TaskState.REJECTED)
            self.assertTrue(terminal.terminal)
            self.assertEqual(terminal.allowed_next, ("reflect",))
            self.assertIsNone(terminal.blocked_reason)
            self.assertEqual(terminal.approval_evidence_refs, ())
            before = lifecycle.ledger.event_count()
            historical = self._record_hypothesis(
                lifecycle,
                task.task_id,
                suffix="after-approval-3",
                status="rejected",
                evidence_ref=evidence,
            )
            self.assertEqual(historical, terminal)
            self.assertEqual(lifecycle.ledger.event_count(), before)

    def test_hypothesis_block_recovers_after_transition_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle, task = self._begin(root)
            task = self._start_hypothesis(lifecycle, task, "crash")
            earlier_evidence = {}
            for index in range(1, 3):
                suffix = f"crash-{index}"
                evidence = self._hypothesis_evidence(
                    lifecycle,
                    task.task_id,
                    suffix,
                )
                earlier_evidence[suffix] = evidence
                task = self._record_hypothesis(
                    lifecycle,
                    task.task_id,
                    suffix=suffix,
                    status="rejected",
                    evidence_ref=evidence,
                )
            final_evidence = self._hypothesis_evidence(
                lifecycle,
                task.task_id,
                "crash-3",
            )
            with patch.object(
                lifecycle.runtime,
                "should_stop",
                side_effect=RuntimeError("simulated escalation crash"),
            ), self.assertRaises(RuntimeError):
                self._record_hypothesis(
                    lifecycle,
                    task.task_id,
                    suffix="crash-3",
                    status="rejected",
                    evidence_ref=final_evidence,
                )

            fresh = self._lifecycle(root)
            blocked = fresh.get(task.task_id)
            self.assertEqual(blocked.state, TaskState.BLOCKED)
            self.assertEqual(blocked.approval_evidence_refs, ())
            self.assertEqual(blocked.allowed_next, ("hypothesis", "reject"))
            self.assertEqual(
                blocked.to_dict()["recommended_next"]["operation"],
                "hypothesis",
            )
            self.assertTrue(
                blocked.to_dict()["recommended_next"]["replay_required"]
            )
            self.assertEqual(
                blocked.blocked_reason,
                "workflow recovery requires an exact operation replay",
            )
            before = fresh.ledger.event_count()
            historical = self._record_hypothesis(
                fresh,
                task.task_id,
                suffix="crash-1",
                status="rejected",
                evidence_ref=earlier_evidence["crash-1"],
            )
            self.assertEqual(historical, blocked)
            self.assertEqual(fresh.ledger.event_count(), before)
            with self.assertRaises(TaskLifecycleError) as premature:
                fresh.approve(
                    task.task_id,
                    stage="phase",
                    phase_id=HYPOTHESIS,
                    approved=True,
                    approver="operator",
                    rationale="must replay the interrupted hypothesis first",
                    evidence_refs=(),
                    idempotency_key="premature-crash-approval",
                    proof="trusted",
                )
            self.assertEqual(premature.exception.code, "recovery_required")
            self.assertEqual(fresh.ledger.event_count(), before)

            recovered = self._record_hypothesis(
                fresh,
                task.task_id,
                suffix="crash-3",
                status="rejected",
                evidence_ref=final_evidence,
            )
            self.assertEqual(recovered.state, TaskState.BLOCKED)
            self.assertEqual(recovered.hypothesis_count, 3)
            self.assertEqual(len(recovered.approval_evidence_refs), 4)
            self.assertEqual(fresh.ledger.event_count(), before + 2)
            gates = [
                event
                for event in fresh.ledger.events_for_contract(
                    task.task_id,
                    all_segments=True,
                )
                if event.event_type == "gate_decision"
                and event.payload.get("operation") == "hypothesis"
            ]
            self.assertEqual(len(gates), 1)

    def test_hypothesis_threshold_comes_from_frozen_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle = self._lifecycle(root)
            task = lifecycle.begin(
                self._contract(max_failed_hypotheses=2),
                idempotency_key="custom-threshold",
                workflow="root-cause-protocol",
            )
            task = self._start_hypothesis(lifecycle, task, "custom-threshold")
            for index in (1, 2):
                suffix = f"custom-threshold-{index}"
                evidence = self._hypothesis_evidence(
                    lifecycle,
                    task.task_id,
                    suffix,
                )
                task = self._record_hypothesis(
                    lifecycle,
                    task.task_id,
                    suffix=suffix,
                    status="rejected",
                    evidence_ref=evidence,
                )

            self.assertEqual(task.hypothesis_count, 2)
            self.assertEqual(task.state, TaskState.BLOCKED)

    def test_incomplete_workflow_cannot_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle, task = self._begin(root)
            before = lifecycle.ledger.event_count()

            with self.assertRaises(TaskLifecycleError) as caught:
                lifecycle.complete(task.task_id, idempotency_key="too-early")

            self.assertEqual(caught.exception.code, "workflow_incomplete")
            self.assertEqual(lifecycle.ledger.event_count(), before)

    def test_auto_trivial_task_remains_unphased(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle = self._lifecycle(root)
            task = lifecycle.begin(
                self._contract("summarize this note"),
                idempotency_key="legacy-auto",
                workflow="auto",
            )

            self.assertEqual(task.workflow, "auto")
            self.assertEqual(task.workflow_phases, ())
            self.assertIsNone(task.current_phase_id)


if __name__ == "__main__":
    unittest.main()
