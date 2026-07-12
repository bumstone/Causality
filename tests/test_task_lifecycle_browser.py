from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Mapping, Sequence
from unittest.mock import patch

from causality.browser_adapter import (
    A11yBrowserAdapter,
    CommandResult,
    REQUIRED_BROWSER_OPERATIONS,
)
from causality.contracts import (
    AuditEventType,
    EvidenceKind,
    EvidenceRequirement,
    GoalContract,
    PermissionContract,
)
from causality.task_lifecycle import (
    TaskActionReceipt,
    TaskLifecycle,
    TaskLifecycleError,
    TaskPolicy,
    TaskState,
)


BROWSER_TOOLS = frozenset(
    {
        "browser.observe",
        "browser.act",
        "browser.assert",
        "browser.inspect",
        "browser.visual",
    }
)
ORIGIN = "https://example.test"
PAGE_SECRET = "page-secret-004b"
FILL_SECRET = "fill-secret-004b"
APPROVAL_SECRET = "approval-secret-004b"


class FakeBrowserDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.environments: list[dict[str, str]] = []
        self.effects = 0
        self.fail_next_action = False
        self.states: dict[str, str] = {}

    def __call__(
        self, command: Sequence[str], environment: Mapping[str, str]
    ) -> CommandResult:
        operation = command[1]
        session = environment.get("CAUSALITY_BROWSER_SESSION_ID")
        self.calls.append((operation, session))
        self.environments.append(dict(environment))
        if operation == "capabilities":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "protocol_version": 1,
                        "session_isolation": True,
                        "network_scope_enforcement": True,
                        "operations": sorted(REQUIRED_BROWSER_OPERATIONS),
                    }
                ),
                "",
            )
        assert session is not None
        state = self.states.setdefault(
            session,
            f'@e1 [button] "Before {PAGE_SECRET}"\n@e2 [textbox] "Email"',
        )
        if operation == "snapshot":
            if "-c" in command:
                return CommandResult(0, f'@e1 [button] "Compact {PAGE_SECRET}"', "")
            if "-s" in command:
                return CommandResult(0, f'@e1 [button] "Scoped {PAGE_SECRET}"', "")
            return CommandResult(0, state, "")
        if operation in {"click", "fill", "hover", "press", "select"}:
            if self.fail_next_action:
                self.fail_next_action = False
                return CommandResult(7, "", f"driver stderr {FILL_SECRET}")
            self.effects += 1
            self.states[session] = (
                f'@e1 [button] "After {PAGE_SECRET}"\n@e2 [textbox] "Email"'
            )
            return CommandResult(0, "acted", "")
        if operation == "console":
            output = "" if self.effects == 0 else "console-secret-delta"
            return CommandResult(0, output, "")
        if operation == "network":
            output = "" if self.effects == 0 else f"{ORIGIN}/sent?network-secret"
            return CommandResult(0, output, "")
        if operation == "is":
            return CommandResult(0, "true", "")
        if operation in {"attrs", "html", "css"}:
            return CommandResult(0, f"inspection {PAGE_SECRET}", "")
        if operation == "screenshot":
            Path(command[-1]).write_bytes(b"fake-png")
            return CommandResult(0, "", "")
        return CommandResult(9, "", "unknown operation")


class BrowserLifecycleTests(unittest.TestCase):
    def _lifecycle(
        self,
        root: Path,
        driver: FakeBrowserDriver,
        *,
        ledger_path: Path | None = None,
    ) -> TaskLifecycle:
        return TaskLifecycle(
            root,
            ledger_path,
            policy=TaskPolicy(
                allowed_tools=BROWSER_TOOLS,
                allowed_network_origins=frozenset({ORIGIN}),
            ),
            approval_authorizer=lambda _who, _stage, proof: proof == APPROVAL_SECRET,
            browser_adapter=A11yBrowserAdapter("fake", runner=driver),
        )

    def _begin(self, lifecycle: TaskLifecycle, key: str = "browser-begin"):
        return lifecycle.begin(
            GoalContract(
                "browser task",
                "isolated fake driver",
                permissions=PermissionContract(
                    allowed_tools=tuple(sorted(BROWSER_TOOLS)),
                    network_scope=(ORIGIN,),
                ),
            ),
            idempotency_key=key,
        )

    def _observe(
        self, lifecycle: TaskLifecycle, task_id: str, key: str = "browser-observe"
    ) -> TaskActionReceipt:
        return lifecycle.perform_action(
            task_id,
            {"kind": "browser", "operation": "observe", "mode": "interactive"},
            idempotency_key=key,
        )

    def _approve(self, lifecycle: TaskLifecycle, task_id: str) -> None:
        lifecycle.approve(
            task_id,
            stage="external_send",
            approved=True,
            approver="operator",
            rationale="allow one controlled DOM action",
            idempotency_key="browser-act-approval",
            proof=APPROVAL_SECRET,
        )

    def test_browser_policy_requires_a_capable_driver(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                TaskLifecycle(
                    temp_dir,
                    policy=TaskPolicy(allowed_tools=frozenset({"browser.observe"})),
                )

            def incapable(
                _command: Sequence[str], _environment: Mapping[str, str]
            ) -> CommandResult:
                return CommandResult(
                    0,
                    json.dumps(
                        {
                            "protocol_version": 1,
                            "session_isolation": True,
                            "network_scope_enforcement": False,
                            "operations": list(BROWSER_TOOLS),
                        }
                    ),
                    "",
                )

            with self.assertRaises(ValueError):
                TaskLifecycle(
                    temp_dir,
                    policy=TaskPolicy(allowed_tools=frozenset({"browser.observe"})),
                    browser_adapter=A11yBrowserAdapter("fake", runner=incapable),
                )

    def test_observe_records_hashes_but_keeps_page_text_out_of_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            driver = FakeBrowserDriver()
            previous = os.environ.get("CAUSALITY_APPROVAL_TOKEN")
            os.environ["CAUSALITY_APPROVAL_TOKEN"] = APPROVAL_SECRET
            try:
                lifecycle = self._lifecycle(root, driver)
                task = self._begin(lifecycle)
                receipt = self._observe(lifecycle, task.task_id)
            finally:
                if previous is None:
                    os.environ.pop("CAUSALITY_APPROVAL_TOKEN", None)
                else:
                    os.environ["CAUSALITY_APPROVAL_TOKEN"] = previous

            self.assertIn(PAGE_SECRET, receipt.ephemeral["snapshot"])
            self.assertNotIn("snapshot", receipt.response)
            self.assertEqual(receipt.response["operation"], "observe")
            cache = receipt.response["cache"]
            self.assertEqual(
                hashlib.sha256(Path(cache["path"]).read_bytes()).hexdigest(),
                cache["sha256"],
            )
            ledger_text = lifecycle.ledger.path.read_text(encoding="utf-8")
            self.assertNotIn(PAGE_SECRET, ledger_text)
            self.assertNotIn(APPROVAL_SECRET, ledger_text)
            self.assertTrue(
                all(
                    "CAUSALITY_APPROVAL_TOKEN" not in environment
                    for environment in driver.environments
                )
            )
            session_environments = [
                environment
                for environment in driver.environments
                if "CAUSALITY_BROWSER_SESSION_ID" in environment
            ]
            self.assertEqual(
                json.loads(
                    session_environments[-1][
                        "CAUSALITY_BROWSER_ALLOWED_ORIGINS_JSON"
                    ]
                ),
                [ORIGIN],
            )
            kinds = [
                event.payload.get("kind")
                for event in lifecycle.ledger.events_for_contract(
                    task.task_id, all_segments=True
                )
                if event.event_type == AuditEventType.EVIDENCE.value
            ]
            self.assertIn("a11y_report", kinds)

    def test_act_requires_approval_then_binds_ref_to_latest_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            driver = FakeBrowserDriver()
            lifecycle = self._lifecycle(root, driver)
            task = self._begin(lifecycle)
            observed = self._observe(lifecycle, task.task_id)
            state_hash = observed.response["state_hash"]
            action = {
                "kind": "browser",
                "operation": "act",
                "action": "fill",
                "ref": "@e2",
                "value": FILL_SECRET,
                "expected_state_hash": state_hash,
            }

            calls_before = len(driver.calls)
            with self.assertRaises(TaskLifecycleError) as blocked:
                lifecycle.perform_action(
                    task.task_id, action, idempotency_key="browser-act"
                )
            self.assertEqual(blocked.exception.code, "approval_required")
            self.assertEqual(len(driver.calls), calls_before)
            self.assertFalse(
                any(
                    event.event_type == AuditEventType.TASK_ACTION_INTENT.value
                    and event.payload.get("idempotency_key") == "browser-act"
                    for event in lifecycle.ledger.events_for_contract(
                        task.task_id, all_segments=True
                    )
                )
            )

            self._approve(lifecycle, task.task_id)
            receipt = lifecycle.perform_action(
                task.task_id, action, idempotency_key="browser-act"
            )

            self.assertEqual(driver.effects, 1)
            self.assertNotEqual(receipt.response["before_state_hash"], receipt.response["after_state_hash"])
            self.assertIn(PAGE_SECRET, receipt.ephemeral["after_snapshot"])
            self.assertIn("console-secret", receipt.ephemeral["console_delta"])
            self.assertIn("network-secret", receipt.ephemeral["network_delta"])
            ledger_text = lifecycle.ledger.path.read_text(encoding="utf-8")
            for secret in (FILL_SECRET, PAGE_SECRET, "console-secret", "network-secret"):
                self.assertNotIn(secret, ledger_text)
            self.assertIn(hashlib.sha256(FILL_SECRET.encode()).hexdigest(), ledger_text)
            kinds = [
                event.payload.get("kind")
                for event in lifecycle.ledger.events_for_contract(
                    task.task_id, all_segments=True
                )
                if event.event_type == AuditEventType.EVIDENCE.value
            ]
            self.assertIn("browser_diff", kinds)
            self.assertEqual(kinds[-1], "a11y_report")

    def test_stale_state_and_missing_ref_fail_before_intent_or_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            driver = FakeBrowserDriver()
            lifecycle = self._lifecycle(root, driver)
            task = self._begin(lifecycle)
            observed = self._observe(lifecycle, task.task_id)
            self._approve(lifecycle, task.task_id)

            for suffix, state_hash, ref in (
                ("stale", "0" * 64, "@e1"),
                ("missing", observed.response["state_hash"], "@e99"),
            ):
                before_effects = driver.effects
                with self.subTest(suffix=suffix), self.assertRaises(
                    TaskLifecycleError
                ) as caught:
                    lifecycle.perform_action(
                        task.task_id,
                        {
                            "kind": "browser",
                            "operation": "act",
                            "action": "click",
                            "ref": ref,
                            "expected_state_hash": state_hash,
                        },
                        idempotency_key=f"browser-{suffix}",
                    )
                self.assertEqual(caught.exception.code, "browser_state_mismatch")
                self.assertEqual(driver.effects, before_effects)
                self.assertFalse(
                    any(
                        event.event_type == AuditEventType.TASK_ACTION_INTENT.value
                        and event.payload.get("idempotency_key") == f"browser-{suffix}"
                        for event in lifecycle.ledger.events_for_contract(
                            task.task_id, all_segments=True
                        )
                    )
                )

    def test_compact_observation_uses_canonical_state_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            driver = FakeBrowserDriver()
            lifecycle = self._lifecycle(root, driver)
            task = self._begin(lifecycle)
            observed = lifecycle.perform_action(
                task.task_id,
                {"kind": "browser", "operation": "observe", "mode": "compact"},
                idempotency_key="browser-compact",
            )
            self.assertNotEqual(
                observed.response["snapshot_hash"], observed.response["state_hash"]
            )
            self._approve(lifecycle, task.task_id)

            acted = lifecycle.perform_action(
                task.task_id,
                {
                    "kind": "browser",
                    "operation": "act",
                    "action": "click",
                    "ref": "@e1",
                    "expected_state_hash": observed.response["state_hash"],
                },
                idempotency_key="browser-act",
            )
            self.assertEqual(acted.response["before_state_hash"], observed.response["state_hash"])

    def test_null_ref_and_narrowed_restart_policy_fail_before_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / ".causality" / "ledger.jsonl"
            driver = FakeBrowserDriver()
            lifecycle = self._lifecycle(root, driver, ledger_path=ledger_path)
            task = self._begin(lifecycle)
            observed = self._observe(lifecycle, task.task_id)
            self._approve(lifecycle, task.task_id)

            with self.assertRaises(TaskLifecycleError) as missing_ref:
                lifecycle.perform_action(
                    task.task_id,
                    {
                        "kind": "browser",
                        "operation": "act",
                        "action": "click",
                        "ref": None,
                        "expected_state_hash": observed.response["state_hash"],
                    },
                    idempotency_key="browser-null-ref",
                )
            self.assertEqual(missing_ref.exception.code, "validation_error")

            restarted = TaskLifecycle(
                root,
                ledger_path,
                policy=TaskPolicy(
                    allowed_tools=BROWSER_TOOLS,
                    allowed_network_origins=frozenset(),
                ),
                approval_authorizer=lambda _who, _stage, proof: proof == APPROVAL_SECRET,
                browser_adapter=A11yBrowserAdapter("fake", runner=driver),
            )
            call_count = len(driver.calls)
            with self.assertRaises(TaskLifecycleError) as narrowed:
                restarted.perform_action(
                    task.task_id,
                    {"kind": "browser", "operation": "observe"},
                    idempotency_key="browser-after-policy-narrow",
                )
            self.assertEqual(narrowed.exception.code, "policy_denied")
            self.assertEqual(len(driver.calls), call_count)

    def test_completed_replay_rehydrates_cache_without_driver_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / ".causality" / "ledger.jsonl"
            driver = FakeBrowserDriver()
            first = self._lifecycle(root, driver, ledger_path=ledger_path)
            task = self._begin(first)
            observed = self._observe(first, task.task_id)
            self._approve(first, task.task_id)
            action = {
                "kind": "browser",
                "operation": "act",
                "action": "click",
                "ref": "@e1",
                "expected_state_hash": observed.response["state_hash"],
            }
            original = first.perform_action(
                task.task_id, action, idempotency_key="browser-act"
            )
            call_count = len(driver.calls)

            restarted = self._lifecycle(root, driver, ledger_path=ledger_path)
            replay = restarted.perform_action(
                task.task_id, action, idempotency_key="browser-act"
            )

            self.assertTrue(replay.replayed)
            self.assertEqual(driver.effects, 1)
            self.assertEqual(len(driver.calls), call_count + 1)  # restart handshake only
            self.assertEqual(replay.response, original.response)
            self.assertEqual(replay.ephemeral, original.ephemeral)

            Path(replay.response["cache"]["path"]).write_text(
                "tampered", encoding="utf-8"
            )
            with self.assertRaises(TaskLifecycleError) as caught:
                restarted.perform_action(
                    task.task_id, action, idempotency_key="browser-act"
                )
            self.assertEqual(caught.exception.code, "browser_cache_invalid")
            self.assertEqual(driver.effects, 1)

    def test_assert_inspect_and_visual_are_state_bound_and_artifact_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            driver = FakeBrowserDriver()
            lifecycle = self._lifecycle(root, driver)
            task = self._begin(lifecycle)
            observed = self._observe(lifecycle, task.task_id)
            state_hash = observed.response["state_hash"]

            asserted = lifecycle.perform_action(
                task.task_id,
                {
                    "kind": "browser",
                    "operation": "assert",
                    "property": "visible",
                    "ref": "@e1",
                    "expected_state_hash": state_hash,
                },
                idempotency_key="browser-assert",
            )
            inspected = lifecycle.perform_action(
                task.task_id,
                {
                    "kind": "browser",
                    "operation": "inspect",
                    "inspection": "attrs",
                    "ref": "@e1",
                    "expected_state_hash": state_hash,
                },
                idempotency_key="browser-inspect",
            )
            visual = lifecycle.perform_action(
                task.task_id,
                {
                    "kind": "browser",
                    "operation": "visual",
                    "ref": "@e1",
                    "expected_state_hash": state_hash,
                },
                idempotency_key="browser-visual",
            )

            self.assertTrue(asserted.response["passed"])
            self.assertIn(PAGE_SECRET, inspected.ephemeral["inspection"])
            artifact = Path(visual.response["artifact"]["path"])
            self.assertTrue(artifact.is_file())
            self.assertTrue(
                artifact.resolve().is_relative_to(
                    (root / ".causality" / "browser" / "artifacts").resolve()
                )
            )
            self.assertNotIn(PAGE_SECRET, lifecycle.ledger.path.read_text(encoding="utf-8"))

    def test_failed_effect_leaves_uncertain_intent_and_never_replays(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / ".causality" / "ledger.jsonl"
            driver = FakeBrowserDriver()
            lifecycle = self._lifecycle(root, driver, ledger_path=ledger_path)
            task = self._begin(lifecycle)
            observed = self._observe(lifecycle, task.task_id)
            self._approve(lifecycle, task.task_id)
            driver.fail_next_action = True
            action = {
                "kind": "browser",
                "operation": "act",
                "action": "fill",
                "ref": "@e2",
                "value": FILL_SECRET,
                "expected_state_hash": observed.response["state_hash"],
            }

            with self.assertRaises(TaskLifecycleError) as failed:
                lifecycle.perform_action(
                    task.task_id, action, idempotency_key="browser-failed"
                )
            self.assertEqual(failed.exception.code, "action_failed")
            self.assertNotIn(FILL_SECRET, failed.exception.message)
            calls_after_failure = len(driver.calls)

            restarted = self._lifecycle(root, driver, ledger_path=ledger_path)
            with self.assertRaises(TaskLifecycleError) as replay:
                restarted.perform_action(
                    task.task_id, action, idempotency_key="browser-failed"
                )
            self.assertIn(
                replay.exception.code,
                {"task_blocked", "unresolved_action_intent"},
            )
            self.assertEqual(len(driver.calls), calls_after_failure + 1)
            self.assertEqual(driver.effects, 0)
            self.assertNotIn(FILL_SECRET, ledger_path.read_text(encoding="utf-8"))

    def test_browser_evidence_satisfies_completion_with_two_cited_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            driver = FakeBrowserDriver()
            lifecycle = self._lifecycle(root, driver)
            task = lifecycle.begin(
                GoalContract(
                    "browser completion",
                    "consume generated browser evidence",
                    permissions=PermissionContract(
                        allowed_tools=tuple(sorted(BROWSER_TOOLS)),
                        network_scope=(ORIGIN,),
                    ),
                    evidence_required=(
                        EvidenceRequirement(
                            EvidenceKind.BROWSER_DIFF, "state transition diff"
                        ),
                        EvidenceRequirement(
                            EvidenceKind.A11Y_REPORT, "post-action accessibility state"
                        ),
                    ),
                ),
                idempotency_key="browser-completion-begin",
            )
            observed = self._observe(lifecycle, task.task_id)
            self._approve(lifecycle, task.task_id)
            lifecycle.perform_action(
                task.task_id,
                {
                    "kind": "browser",
                    "operation": "act",
                    "action": "click",
                    "ref": "@e1",
                    "expected_state_hash": observed.response["state_hash"],
                },
                idempotency_key="browser-completion-act",
            )
            evidence = [
                event.entry_hash
                for event in lifecycle.ledger.events_for_contract(
                    task.task_id, all_segments=True
                )
                if event.event_type == AuditEventType.EVIDENCE.value
                and event.payload.get("kind") in {"browser_diff", "a11y_report"}
            ][-2:]
            for index in (1, 2):
                lifecycle.verdict(
                    task.task_id,
                    verifier=f"browser-review-{index}",
                    status="pass",
                    rationale="browser lifecycle evidence is consistent",
                    evidence_refs=tuple(evidence),
                    idempotency_key=f"browser-verdict-{index}",
                )

            completed = lifecycle.complete(
                task.task_id, idempotency_key="browser-complete"
            )
            self.assertEqual(completed.state, TaskState.VERIFIED)

    def test_cache_parent_swap_is_detected_and_outside_file_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside"
            outside.mkdir()
            probe = root / "symlink-probe"
            try:
                os.symlink(outside, probe, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks are unavailable")
            else:
                probe.unlink()

            driver = FakeBrowserDriver()
            lifecycle = self._lifecycle(root, driver)
            task = self._begin(lifecycle)
            real_mkstemp = tempfile.mkstemp
            swapped = False

            def swapping_mkstemp(*args, **kwargs):
                nonlocal swapped
                directory = Path(kwargs["dir"])
                if not swapped and directory.name:
                    moved = directory.with_name(directory.name + "-moved")
                    directory.rename(moved)
                    os.symlink(outside, directory, target_is_directory=True)
                    swapped = True
                return real_mkstemp(*args, **kwargs)

            with patch(
                "causality.task_lifecycle._browser_mkstemp",
                side_effect=swapping_mkstemp,
            ), self.assertRaises(TaskLifecycleError) as caught:
                self._observe(lifecycle, task.task_id)

            self.assertEqual(caught.exception.code, "browser_runtime_invalid")
            self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
