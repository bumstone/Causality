from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from causality.contracts import (
    AuditEventType,
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


class WorkflowLifecycleTests(unittest.TestCase):
    def _lifecycle(self, root: Path) -> TaskLifecycle:
        return TaskLifecycle(
            root,
            policy=TaskPolicy(verification_commands=(COMMAND,)),
            effect_runner=lambda _action: {"status": "ok"},
        )

    @staticmethod
    def _contract(title: str = "debug checkout") -> GoalContract:
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
                "max_failed_hypotheses": 3,
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
            self.assertEqual(
                self._lifecycle(root).get(task.task_id).to_dict(),
                task.to_dict(),
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

    def test_projection_rejects_empty_auto_plan_for_nontrivial_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle = self._lifecycle(root)
            contract = self._contract()
            key = "forged-auto"
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
            request.update({"workflow": "auto", "phase_plan": []})
            lifecycle.ledger.append(
                AuditEventType.TASK_STARTED,
                {
                    "schema_version": 1,
                    "task_id": task_id,
                    "idempotency_key": key,
                    "request_sha256": canonical_sha256(request),
                    "request": request,
                    "workflow": "auto",
                    "phase_plan": [],
                    "response": {
                        "task_id": task_id,
                        "contract_id": task_id,
                        "workflow": "auto",
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

            with self.assertRaises(TaskLifecycleError) as caught:
                lifecycle.action(
                    task.task_id,
                    {"kind": "file_write", "path": "out/result.txt", "content": "x"},
                    idempotency_key="early-effect",
                )

            self.assertEqual(caught.exception.code, "phase_not_running")
            self.assertEqual(calls, [])
            self.assertEqual(lifecycle.ledger.event_count(), before)

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
