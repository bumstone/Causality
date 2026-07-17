from __future__ import annotations

import copy
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import (
    AuditEventType,
    EvidenceKind,
    GoalContract,
    PermissionContract,
    Risk,
    VerificationRequirement,
)
from causality.ledger import EvidenceLedger
from causality.memory import TypedMemory
from causality.task_lifecycle import (
    TaskLifecycle,
    TaskLifecycleError,
    TaskPolicy,
    TaskState,
)


ACTION_POLICY = TaskPolicy(
    subprocess_argv_prefixes=((sys.executable, "-c"),),
)


class SimulatedProcessDeath(BaseException):
    """Leave a durable intent without letting ordinary error handling add a result."""


class TaskLifecycleTests(unittest.TestCase):
    def _contract(self, title: str = "task", *, risk: Risk = Risk.MEDIUM) -> GoalContract:
        return GoalContract(
            title=title,
            summary=f"execute {title}",
            risk=risk,
            permissions=PermissionContract(
                allowed_tools=("shell", "file.read", "file.write"),
                write_scope=("out",),
            ),
            non_goals=("delete production",),
        )

    @staticmethod
    def _action(label: str = "one") -> dict[str, object]:
        return {
            "kind": "subprocess",
            "argv": [sys.executable, "-c", f"print({label!r})"],
            "cwd": ".",
        }

    @staticmethod
    def _events(path: Path, task_id: str):
        return EvidenceLedger(path).events_for_contract(task_id, all_segments=True)

    @staticmethod
    def _task_operations(path: Path, task_id: str, operation: str):
        event_types = {
            "begin": "task_started",
            "action_intent": "task_action_intent",
            "action_result": "task_action_result",
        }
        return [
            event
            for event in TaskLifecycleTests._events(path, task_id)
            if event.event_type == event_types[operation]
        ]

    def test_begin_uses_one_identity_for_task_and_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / ".causality" / "ledger.jsonl"
            lifecycle = TaskLifecycle(root, ledger_path)
            contract = self._contract()

            session = lifecycle.begin(contract, idempotency_key="begin-identity")

            self.assertEqual(session.task_id, session.contract_id)
            snapshot = EvidenceLedger(ledger_path).contract_snapshot(session.task_id)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["goal_id"], session.task_id)
            self.assertEqual(snapshot["workspace_root"], str(root.resolve()))
            self.assertEqual(lifecycle.get(session.task_id), session)
            self.assertEqual(lifecycle.session(session.task_id), session)

    def test_raw_transition_api_is_not_exposed(self) -> None:
        self.assertFalse(
            hasattr(TaskLifecycle, "transition"),
            "state changes must be owned by typed lifecycle operations and gates",
        )

    def test_begin_idempotency_is_project_scoped_and_conflicts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            first_root = base / "first"
            second_root = base / "second"
            first_root.mkdir()
            second_root.mkdir()
            ledger_path = first_root / ".causality" / "ledger.jsonl"
            original = self._contract("same request")
            # Caller-generated goal_id/created_at values are transport metadata;
            # the project-scoped begin key owns the durable task identity.
            retry_request = self._contract("same request")

            first = TaskLifecycle(first_root, ledger_path).begin(
                original, idempotency_key="project-key"
            )
            count = EvidenceLedger(ledger_path).event_count()
            replay = TaskLifecycle(first_root, ledger_path).begin(
                retry_request, idempotency_key="project-key"
            )
            self.assertEqual(replay, first)
            self.assertEqual(EvidenceLedger(ledger_path).event_count(), count)

            with self.assertRaises(TaskLifecycleError) as caught:
                TaskLifecycle(first_root, ledger_path).begin(
                    self._contract("different request"),
                    idempotency_key="project-key",
                )
            self.assertIn("idempot", str(caught.exception).lower())

            # A begin key reserves identity only inside one project ledger.
            other = TaskLifecycle(second_root).begin(
                self._contract("other project"), idempotency_key="project-key"
            )
            self.assertNotEqual(other.task_id, first.task_id)

    def test_concurrent_same_key_begin_is_one_durable_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / ".causality" / "ledger.jsonl"
            requests = [self._contract("concurrent begin") for _ in range(4)]
            barrier = threading.Barrier(len(requests))
            results: list[object] = []
            errors: list[BaseException] = []
            result_lock = threading.Lock()

            def begin(request: GoalContract) -> None:
                try:
                    barrier.wait()
                    result = TaskLifecycle(root, ledger_path).begin(
                        request, idempotency_key="one-concurrent-begin"
                    )
                    with result_lock:
                        results.append(result)
                except BaseException as exc:  # collect worker failures for the main thread
                    with result_lock:
                        errors.append(exc)

            threads = [
                threading.Thread(target=begin, args=(request,), daemon=True)
                for request in requests
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 4)
            self.assertEqual(len({result.task_id for result in results}), 1)
            events = EvidenceLedger(ledger_path).events(all_segments=True)
            self.assertEqual(
                sum(event.event_type == "goal_contract" for event in events), 1
            )
            self.assertEqual(
                sum(
                    event.event_type == "task_started"
                    for event in events
                ),
                1,
            )

    def test_action_same_key_replays_result_and_conflicting_request_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            calls: list[dict[str, object]] = []

            def run_effect(action: dict[str, object]) -> dict[str, object]:
                calls.append(action)
                return {"status": "ok", "call": len(calls)}

            lifecycle = TaskLifecycle(root, effect_runner=run_effect, policy=ACTION_POLICY)
            task = lifecycle.begin(self._contract(), idempotency_key="begin-action")
            request = self._action()
            first = lifecycle.action(
                task.task_id, request, idempotency_key="action-key"
            )
            ledger = EvidenceLedger(root / ".causality" / "ledger.jsonl")
            count = ledger.event_count()

            replay = lifecycle.action(
                task.task_id, copy.deepcopy(request), idempotency_key="action-key"
            )
            self.assertEqual(replay, first)
            self.assertEqual(len(calls), 1)
            self.assertEqual(ledger.event_count(), count)

            with self.assertRaises(TaskLifecycleError) as caught:
                lifecycle.action(
                    task.task_id,
                    self._action("changed"),
                    idempotency_key="action-key",
                )
            self.assertIn("idempot", str(caught.exception).lower())
            self.assertEqual(len(calls), 1)

    def test_concurrent_same_action_key_executes_effect_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / ".causality" / "ledger.jsonl"
            effect_calls: list[dict[str, object]] = []
            effect_lock = threading.Lock()

            def run_effect(action: dict[str, object]) -> dict[str, object]:
                with effect_lock:
                    effect_calls.append(action)
                    return {"status": "ok", "call": len(effect_calls)}

            task = TaskLifecycle(
                root, ledger_path, effect_runner=run_effect, policy=ACTION_POLICY
            ).begin(
                self._contract("concurrent action"),
                idempotency_key="begin-concurrent-action",
            )
            barrier = threading.Barrier(4)
            results: list[object] = []
            errors: list[BaseException] = []
            result_lock = threading.Lock()
            request = self._action()

            def act() -> None:
                try:
                    lifecycle = TaskLifecycle(
                        root,
                        ledger_path,
                        effect_runner=run_effect,
                        policy=ACTION_POLICY,
                    )
                    barrier.wait()
                    result = lifecycle.action(
                        task.task_id,
                        copy.deepcopy(request),
                        idempotency_key="one-concurrent-action",
                    )
                    with result_lock:
                        results.append(result)
                except BaseException as exc:
                    with result_lock:
                        errors.append(exc)

            threads = [threading.Thread(target=act, daemon=True) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 4)
            self.assertTrue(all(result == results[0] for result in results))
            self.assertEqual(len(effect_calls), 1)
            self.assertEqual(
                len(
                    self._task_operations(
                        ledger_path, task.task_id, "action_intent"
                    )
                ),
                1,
            )
            self.assertEqual(
                len(
                    self._task_operations(
                        ledger_path, task.task_id, "action_result"
                    )
                ),
                1,
            )

    def test_reconstruction_folds_all_rotated_segments_and_preserves_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / ".causality" / "ledger.jsonl"
            calls: list[dict[str, object]] = []

            def run_effect(action: dict[str, object]) -> dict[str, object]:
                calls.append(action)
                return {"status": "ok", "call": len(calls)}

            lifecycle = TaskLifecycle(
                root, ledger_path, effect_runner=run_effect, policy=ACTION_POLICY
            )
            task = lifecycle.begin(self._contract(), idempotency_key="begin-rotate")
            EvidenceLedger(ledger_path).rotate()
            request = self._action()
            first_result = lifecycle.action(
                task.task_id, request, idempotency_key="rotated-action"
            )
            EvidenceLedger(ledger_path).rotate()

            fresh = TaskLifecycle(
                root, ledger_path, effect_runner=run_effect, policy=ACTION_POLICY
            )
            restored = fresh.get(task.task_id)
            self.assertEqual(restored.task_id, task.task_id)
            self.assertEqual(restored.state, TaskState.EXECUTING)
            count = EvidenceLedger(ledger_path).event_count()
            replay = fresh.action(
                task.task_id, request, idempotency_key="rotated-action"
            )
            self.assertEqual(replay, first_result)
            self.assertEqual(len(calls), 1)
            self.assertEqual(EvidenceLedger(ledger_path).event_count(), count)
            self.assertTrue(EvidenceLedger(ledger_path).verify_chain())

    def test_intent_precedes_effect_and_orphan_is_blocked_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / ".causality" / "ledger.jsonl"
            calls: list[dict[str, object]] = []
            task_id = ""

            def die_during_effect(action: dict[str, object]) -> dict[str, object]:
                calls.append(action)
                intents = self._task_operations(ledger_path, task_id, "action_intent")
                self.assertEqual(len(intents), 1, "intent must be durable before effect")
                self.assertEqual(
                    self._task_operations(ledger_path, task_id, "action_result"), []
                )
                raise SimulatedProcessDeath("crash after external effect may have begun")

            lifecycle = TaskLifecycle(
                root,
                ledger_path,
                effect_runner=die_during_effect,
                policy=ACTION_POLICY,
            )
            task = lifecycle.begin(self._contract(), idempotency_key="begin-orphan")
            task_id = task.task_id
            request = self._action()
            with self.assertRaises(SimulatedProcessDeath):
                lifecycle.action(task_id, request, idempotency_key="orphan-action")

            fresh = TaskLifecycle(
                root,
                ledger_path,
                effect_runner=die_during_effect,
                policy=ACTION_POLICY,
            )
            blocked = fresh.get(task_id)
            self.assertEqual(blocked.state, TaskState.BLOCKED)
            self.assertEqual(len(blocked.unresolved_intents), 1)
            self.assertIsNotNone(blocked.unresolved_intents[0].operation_id)
            before = EvidenceLedger(ledger_path).event_count()
            with self.assertRaises(TaskLifecycleError):
                fresh.action(task_id, request, idempotency_key="orphan-action")
            self.assertEqual(len(calls), 1)

            # Repeated recovery reads are pure and derive only one blocked state.
            self.assertEqual(fresh.get(task_id), blocked)
            self.assertEqual(EvidenceLedger(ledger_path).event_count(), before)

    def test_same_operation_key_is_isolated_between_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / ".causality" / "ledger.jsonl"
            calls: list[dict[str, object]] = []

            def run_effect(action: dict[str, object]) -> dict[str, object]:
                calls.append(action)
                return {"status": "ok", "call": len(calls)}

            lifecycle = TaskLifecycle(
                root, ledger_path, effect_runner=run_effect, policy=ACTION_POLICY
            )
            first = lifecycle.begin(
                self._contract("first"), idempotency_key="begin-first"
            )
            second = lifecycle.begin(
                self._contract("second"), idempotency_key="begin-second"
            )
            lifecycle.action(first.task_id, self._action(), idempotency_key="shared")
            lifecycle.action(second.task_id, self._action(), idempotency_key="shared")

            self.assertEqual(len(calls), 2)
            self.assertNotEqual(first.task_id, second.task_id)
            for task_id in (first.task_id, second.task_id):
                events = self._events(ledger_path, task_id)
                self.assertTrue(events)
                self.assertTrue(all(event.contract_id == task_id for event in events))
                for event in events:
                    embedded = event.payload.get("task_id")
                    if embedded is not None:
                        self.assertEqual(embedded, task_id)

    def test_subprocess_and_verification_require_server_owned_command_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            calls: list[dict[str, object]] = []
            denied = TaskLifecycle(
                root,
                effect_runner=lambda action: calls.append(action) or {"status": "ok"},
            )
            task = denied.begin(self._contract("policy"), idempotency_key="policy-begin")
            with self.assertRaises(TaskLifecycleError) as caught:
                denied.action(task.task_id, self._action(), idempotency_key="policy-action")
            self.assertEqual(caught.exception.code, "policy_denied")
            self.assertEqual(calls, [])

            command = (sys.executable, "-c", "print('allowed exact command')")
            contract = GoalContract(
                title="verification policy",
                summary="",
                permissions=PermissionContract(allowed_tools=("shell",)),
                verification_requirements=(
                    VerificationRequirement(id="allowed", argv=command),
                ),
            )
            with self.assertRaises(TaskLifecycleError) as verify_denied:
                TaskLifecycle(root / "denied").begin(
                    contract,
                    idempotency_key="verify-denied",
                )
            self.assertEqual(verify_denied.exception.code, "policy_denied")
            allowed = TaskLifecycle(
                root / "allowed",
                policy=TaskPolicy(verification_commands=(command,)),
            ).begin(contract, idempotency_key="verify-allowed")
            self.assertEqual(allowed.state, TaskState.PLANNED)

            overlong = GoalContract(
                title="verification timeout policy",
                summary="",
                permissions=PermissionContract(allowed_tools=("shell",)),
                verification_requirements=(
                    VerificationRequirement(
                        id="too-long",
                        argv=command,
                        timeout_seconds=2,
                    ),
                ),
            )
            with self.assertRaises(TaskLifecycleError) as timeout_denied:
                TaskLifecycle(
                    root / "timeout-denied",
                    policy=TaskPolicy(
                        verification_commands=(command,),
                        max_timeout_seconds=1,
                    ),
                ).begin(overlong, idempotency_key="timeout-denied")
            self.assertEqual(timeout_denied.exception.code, "policy_denied")
            self.assertIn("too-long", timeout_denied.exception.details["requirements"])

    def test_manual_verify_operation_returns_cited_evidence_and_decision_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lifecycle = TaskLifecycle(
                root,
                approval_authorizer=lambda _principal, _stage, _proof: True,
            )
            task = lifecycle.begin(
                GoalContract(
                    title="manual verification response",
                    summary="",
                    verification_requirements=(
                        VerificationRequirement(id="visual", argv=(), manual=True),
                    ),
                ),
                idempotency_key="manual-begin",
            )
            contract = lifecycle._contract(task)
            evidence = lifecycle.runtime.record_evidence(
                contract,
                EvidenceKind.A11Y_REPORT,
                {"summary": "screen reviewed"},
            )

            lifecycle.verify(
                task.task_id,
                "visual",
                idempotency_key="manual-verify",
                mode="manual",
                evidence_hash=evidence.entry_hash,
                approved=True,
                approver="reviewer",
                rationale="visual state matches",
                proof="trusted",
            )

            operation = lifecycle.ledger.find(
                AuditEventType.TASK_OPERATION,
                lambda event: event.contract_id == task.task_id
                and event.payload.get("operation") == "verify",
            )[-1]
            response = operation.payload["response"]
            self.assertEqual(response["evidence_hash"], evidence.entry_hash)
            self.assertRegex(response["decision_hash"], r"^[0-9a-f]{64}$")
            self.assertNotEqual(response["decision_hash"], evidence.entry_hash)

    def test_high_risk_final_approval_recovers_completion_escalation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            command = (sys.executable, "-c", "print('verified')")
            lifecycle = TaskLifecycle(
                root,
                policy=TaskPolicy(verification_commands=(command,)),
                approval_authorizer=lambda _principal, _stage, _proof: True,
            )
            task = lifecycle.begin(
                GoalContract(
                    title="high risk recovery",
                    summary="",
                    risk=Risk.HIGH,
                    permissions=PermissionContract(allowed_tools=("shell",)),
                    verification_requirements=(
                        VerificationRequirement(id="pass", argv=command),
                    ),
                ),
                idempotency_key="high-begin",
            )
            task = lifecycle.approve(
                task.task_id,
                stage="plan",
                approved=True,
                approver="operator",
                rationale="reviewed plan",
                idempotency_key="high-plan",
                proof="trusted",
            )
            task = lifecycle.verify(
                task.task_id,
                "pass",
                idempotency_key="high-verify",
            )
            evidence_hash = task.idempotency[("verify", "high-verify")].response[
                "event_hash"
            ]
            for index, verifier in enumerate(("security", "conformance"), start=1):
                task = lifecycle.verdict(
                    task.task_id,
                    verifier=verifier,
                    status="pass",
                    rationale="independent review",
                    evidence_refs=(evidence_hash,),
                    idempotency_key=f"high-verdict-{index}",
                )
            task = lifecycle.complete(task.task_id, idempotency_key="high-before-final")
            self.assertEqual(task.state, TaskState.BLOCKED)
            task = lifecycle.approve(
                task.task_id,
                stage="final",
                approved=True,
                approver="operator",
                rationale="reviewed exact evidence",
                evidence_refs=(evidence_hash,),
                idempotency_key="high-final",
                proof="trusted",
            )
            self.assertEqual(task.state, TaskState.EXECUTING)
            task = lifecycle.complete(task.task_id, idempotency_key="high-complete")
            self.assertEqual(task.state, TaskState.VERIFIED)

    def test_public_lifecycle_approval_defaults_to_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lifecycle = TaskLifecycle(Path(temp_dir))
            task = lifecycle.begin(
                self._contract("fail closed", risk=Risk.HIGH),
                idempotency_key="deny-begin",
            )
            with self.assertRaises(TaskLifecycleError) as denied:
                lifecycle.approve(
                    task.task_id,
                    stage="plan",
                    approved=True,
                    approver="untrusted",
                    rationale="no trust provider is configured",
                    idempotency_key="deny-plan",
                )
            self.assertEqual(denied.exception.code, "approval_required")
            self.assertEqual(lifecycle.get(task.task_id).state, TaskState.PLANNED)

    def test_reflection_crash_after_memory_write_converges_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            command = (sys.executable, "-c", "print('reflection source')")
            policy = TaskPolicy(verification_commands=(command,))
            lifecycle = TaskLifecycle(root, policy=policy)
            task = lifecycle.begin(
                GoalContract(
                    title="reflection crash",
                    summary="",
                    permissions=PermissionContract(allowed_tools=("shell",)),
                    verification_requirements=(
                        VerificationRequirement(id="pass", argv=command),
                    ),
                ),
                idempotency_key="reflection-begin",
            )
            task = lifecycle.verify(
                task.task_id,
                "pass",
                idempotency_key="reflection-verify",
            )
            evidence_hash = task.idempotency[
                ("verify", "reflection-verify")
            ].response["event_hash"]
            for index, verifier in enumerate(("security", "conformance"), start=1):
                task = lifecycle.verdict(
                    task.task_id,
                    verifier=verifier,
                    status="pass",
                    rationale="checked",
                    evidence_refs=(evidence_hash,),
                    idempotency_key=f"reflection-verdict-{index}",
                )
            task = lifecycle.complete(
                task.task_id,
                idempotency_key="reflection-complete",
            )
            original = TypedMemory.record_once
            calls = 0

            def crash_after_write(memory, *args, **kwargs):
                nonlocal calls
                entry = original(memory, *args, **kwargs)
                calls += 1
                if calls == 1:
                    raise SimulatedProcessDeath("crash after durable memory append")
                return entry

            with patch.object(TypedMemory, "record_once", crash_after_write):
                with self.assertRaises(SimulatedProcessDeath):
                    lifecycle.reflect(
                        task.task_id,
                        idempotency_key="reflection-once",
                    )

            log = root / "memory" / "retrospectives" / "log.jsonl"
            self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)
            resumed = TaskLifecycle(root, policy=policy).reflect(
                task.task_id,
                idempotency_key="reflection-once",
            )
            self.assertIsNotNone(resumed.reflection)
            self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)
            self.assertTrue(EvidenceLedger(root / ".causality" / "ledger.jsonl").verify_chain())

    def test_verdict_and_completion_partial_commits_recover_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            command = (sys.executable, "-c", "print('partial commit')")
            policy = TaskPolicy(verification_commands=(command,))
            lifecycle = TaskLifecycle(root, policy=policy)
            task = lifecycle.begin(
                GoalContract(
                    title="partial commits",
                    summary="",
                    permissions=PermissionContract(allowed_tools=("shell",)),
                    verification_requirements=(
                        VerificationRequirement(id="pass", argv=command),
                    ),
                ),
                idempotency_key="partial-begin",
            )
            task = lifecycle.verify(
                task.task_id,
                "pass",
                idempotency_key="partial-verify",
            )
            evidence_hash = task.idempotency[("verify", "partial-verify")].response[
                "event_hash"
            ]
            with patch.object(
                lifecycle,
                "_append_operation",
                side_effect=SimulatedProcessDeath("decision durable, response lost"),
            ):
                with self.assertRaises(SimulatedProcessDeath):
                    lifecycle.verdict(
                        task.task_id,
                        verifier="security",
                        status="pass",
                        rationale="checked",
                        evidence_refs=(evidence_hash,),
                        idempotency_key="partial-verdict",
                    )

            fresh = TaskLifecycle(root, policy=policy)
            task = fresh.verdict(
                task.task_id,
                verifier="security",
                status="pass",
                rationale="checked",
                evidence_refs=(evidence_hash,),
                idempotency_key="partial-verdict",
            )
            verifier_events = [
                event
                for event in fresh.ledger.events_for_contract(
                    task.task_id, all_segments=True
                )
                if event.event_type == "verifier_decision"
                and event.payload.get("verifier") == "security"
            ]
            self.assertEqual(len(verifier_events), 1)
            task = fresh.verdict(
                task.task_id,
                verifier="conformance",
                status="pass",
                rationale="checked",
                evidence_refs=(evidence_hash,),
                idempotency_key="partial-verdict-2",
            )
            original_transition = fresh._append_transition

            def crash_before_terminal(session, target, **kwargs):
                if target is TaskState.VERIFIED:
                    raise SimulatedProcessDeath("completion result durable, state lost")
                return original_transition(session, target, **kwargs)

            with patch.object(fresh, "_append_transition", crash_before_terminal):
                with self.assertRaises(SimulatedProcessDeath):
                    fresh.complete(task.task_id, idempotency_key="partial-complete")
            completed = TaskLifecycle(root, policy=policy).complete(
                task.task_id,
                idempotency_key="partial-complete",
            )
            self.assertEqual(completed.state, TaskState.VERIFIED)
            completion_gates = [
                event
                for event in fresh.ledger.events_for_contract(
                    task.task_id, all_segments=True
                )
                if event.event_type == "gate_decision"
                and event.payload.get("idempotency_key") == "partial-complete"
            ]
            self.assertEqual(len(completion_gates), 1)

    def test_action_and_verification_failure_state_recovers_after_response_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            calls: list[dict[str, object]] = []

            def fail_effect(action):
                calls.append(action)
                raise ValueError("known action failure")

            lifecycle = TaskLifecycle(
                root / "action",
                effect_runner=fail_effect,
                policy=ACTION_POLICY,
            )
            task = lifecycle.begin(
                self._contract("action failure"),
                idempotency_key="failure-begin",
            )
            with self.assertRaises(TaskLifecycleError) as failed:
                lifecycle.action(
                    task.task_id,
                    self._action("failure"),
                    idempotency_key="failure-action",
                )
            self.assertEqual(failed.exception.code, "action_failed")
            recovery = TaskLifecycle(
                root / "action",
                effect_runner=fail_effect,
                policy=ACTION_POLICY,
                approval_authorizer=lambda _principal, _stage, _proof: True,
            )
            blocked = recovery.get(task.task_id)
            self.assertEqual(blocked.state, TaskState.BLOCKED)
            self.assertEqual(len(blocked.unresolved_intents), 1)
            recovered = recovery.resolve(
                task.task_id,
                operation_id=blocked.unresolved_intents[0].operation_id,
                resolution="not_applied",
                approver="operator",
                rationale="the failed call did not apply the effect",
                idempotency_key="failure-resolve",
                proof="trusted",
            )
            self.assertEqual(recovered.state, TaskState.EXECUTING)
            self.assertEqual(len(calls), 1)

            command = (sys.executable, "-c", "import time; time.sleep(1)")
            verify_root = root / "verify"
            verifier = TaskLifecycle(
                verify_root,
                policy=TaskPolicy(verification_commands=(command,)),
            )
            task = verifier.begin(
                GoalContract(
                    title="verification timeout",
                    summary="",
                    permissions=PermissionContract(allowed_tools=("shell",)),
                    verification_requirements=(
                        VerificationRequirement(
                            id="timeout",
                            argv=command,
                            timeout_seconds=0.05,
                        ),
                    ),
                ),
                idempotency_key="timeout-begin",
            )
            original_transition = verifier._append_transition

            def crash_on_verify_block(session, target, **kwargs):
                if target is TaskState.BLOCKED:
                    raise SimulatedProcessDeath("verification result durable, block lost")
                return original_transition(session, target, **kwargs)

            with patch.object(verifier, "_append_transition", crash_on_verify_block):
                with self.assertRaises(SimulatedProcessDeath):
                    verifier.verify(
                        task.task_id,
                        "timeout",
                        idempotency_key="timeout-verify",
                    )
            recovered = TaskLifecycle(
                verify_root,
                policy=TaskPolicy(verification_commands=(command,)),
            ).verify(
                task.task_id,
                "timeout",
                idempotency_key="timeout-verify",
            )
            self.assertEqual(recovered.state, TaskState.BLOCKED)

    def test_terminal_transition_replays_but_new_work_cannot_reopen_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / ".causality" / "ledger.jsonl"
            calls: list[dict[str, object]] = []
            lifecycle = TaskLifecycle(
                root,
                ledger_path,
                effect_runner=lambda action: calls.append(action) or {"status": "ok"},
                policy=ACTION_POLICY,
                approval_authorizer=lambda _principal, _stage, _proof: True,
            )
            task = lifecycle.begin(
                self._contract(risk=Risk.HIGH), idempotency_key="begin-terminal"
            )
            terminal = lifecycle.approve(
                task.task_id,
                stage="plan",
                approved=False,
                approver="operator",
                rationale="operator rejected the plan",
                idempotency_key="reject-task",
                proof="trusted",
            )
            count = EvidenceLedger(ledger_path).event_count()

            replay = lifecycle.approve(
                task.task_id,
                stage="plan",
                approved=False,
                approver="operator",
                rationale="operator rejected the plan",
                idempotency_key="reject-task",
                proof="trusted",
            )
            self.assertEqual(replay, terminal)
            self.assertEqual(EvidenceLedger(ledger_path).event_count(), count)
            with self.assertRaises(TaskLifecycleError):
                lifecycle.action(
                    task.task_id, self._action(), idempotency_key="after-terminal"
                )
            self.assertEqual(calls, [])
            self.assertEqual(lifecycle.get(task.task_id).state, TaskState.REJECTED)

    def _make_orphan(self, root: Path, suffix: str):
        ledger_path = root / ".causality" / "ledger.jsonl"
        calls: list[dict[str, object]] = []

        def crash(action: dict[str, object]) -> dict[str, object]:
            calls.append(action)
            raise SimulatedProcessDeath(suffix)

        lifecycle = TaskLifecycle(
            root, ledger_path, effect_runner=crash, policy=ACTION_POLICY
        )
        task = lifecycle.begin(
            self._contract(f"orphan {suffix}"), idempotency_key=f"begin-{suffix}"
        )
        with self.assertRaises(SimulatedProcessDeath):
            lifecycle.action(
                task.task_id, self._action(suffix), idempotency_key=f"action-{suffix}"
            )
        fresh = TaskLifecycle(
            root,
            ledger_path,
            effect_runner=lambda action: calls.append(action) or {"status": "ok"},
            policy=ACTION_POLICY,
            approval_authorizer=lambda _principal, _stage, _proof: True,
        )
        blocked = fresh.get(task.task_id)
        self.assertEqual(blocked.state, TaskState.BLOCKED)
        return fresh, blocked, calls, ledger_path

    def test_resolve_requires_explicit_outcome_and_only_not_applied_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            for suffix, resolution, expected_state in (
                ("applied", "applied", TaskState.BLOCKED),
                ("not-applied", "not_applied", TaskState.EXECUTING),
                ("reject", "reject", TaskState.REJECTED),
            ):
                with self.subTest(resolution=resolution):
                    root = base / suffix
                    root.mkdir()
                    lifecycle, blocked, calls, ledger_path = self._make_orphan(
                        root, suffix
                    )
                    resolved = lifecycle.resolve(
                        blocked.task_id,
                        operation_id=blocked.unresolved_intents[0].operation_id,
                        resolution=resolution,
                        approver="operator",
                        rationale="checked the external system",
                        idempotency_key=f"resolve-{suffix}",
                    )
                    self.assertEqual(resolved.state, expected_state)
                    count = EvidenceLedger(ledger_path).event_count()
                    replay = lifecycle.resolve(
                        blocked.task_id,
                        operation_id=blocked.unresolved_intents[0].operation_id,
                        resolution=resolution,
                        approver="operator",
                        rationale="checked the external system",
                        idempotency_key=f"resolve-{suffix}",
                    )
                    self.assertEqual(replay, resolved)
                    self.assertEqual(EvidenceLedger(ledger_path).event_count(), count)
                    self.assertEqual(len(calls), 1, "resolve must never replay the effect")

                    if resolution == "not_applied":
                        lifecycle.action(
                            blocked.task_id,
                            self._action("safe-retry"),
                            idempotency_key="new-action-after-resolution",
                        )
                        self.assertEqual(len(calls), 2)
                    else:
                        with self.assertRaises(TaskLifecycleError):
                            lifecycle.action(
                                blocked.task_id,
                                self._action("must-not-run"),
                                idempotency_key="blocked-after-resolution",
                            )
                        self.assertEqual(len(calls), 1)

            invalid_root = base / "invalid"
            invalid_root.mkdir()
            lifecycle, blocked, _, _ = self._make_orphan(invalid_root, "invalid")
            with self.assertRaises(TaskLifecycleError):
                lifecycle.resolve(
                    blocked.task_id,
                    operation_id=blocked.unresolved_intents[0].operation_id,
                    resolution="guess",
                    approver="operator",
                    rationale="guessing is not recovery",
                    idempotency_key="invalid-resolution",
                )


if __name__ == "__main__":
    unittest.main()
