from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import EvidenceKind, GateDecision, VerifierDecision
from causality.durable import DurableJsonl
from causality.engine import CausalityEngine
from causality.memory import MemoryEntry


def _passing_verifiers():
    return [
        lambda c: VerifierDecision("correctness", "pass", "looks right"),
        lambda c: VerifierDecision("evidence", "pass", "evidence present"),
    ]


def _failing_verifiers():
    return [lambda c: VerifierDecision("correctness", "fail", "nope")]


def _evidence_work(engine: CausalityEngine):
    def work(contract, iteration):
        engine.runtime.record_evidence(contract, EvidenceKind.TEST_OUTPUT, {"output": "ok"})

    return work


class GuardrailFeedforwardTests(unittest.TestCase):
    """failures recorded under a stable scope feed forward as confirmed non_goals."""

    def test_failure_feeds_forward_as_confirmed_guardrail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))

            # Run 1 fails, so reflect records failures under the shared scope.
            run1 = engine.run_task(
                objective="parse the grammar",
                work=_evidence_work(engine),
                verifiers=_failing_verifiers(),
                verification=["pytest"],
                stop_condition={"max_iterations": 1, "no_progress_iterations": 99},
                failure_scope="parser-tasks",
            )
            self.assertFalse(run1.passed)
            scoped = [
                e
                for e in engine.memory.entries("failures")
                if e.metadata.get("scope") == "parser-tasks"
            ]
            self.assertTrue(scoped)  # recorded under the stable scope, not contract:<id>

            # Run 2 in the same scope: the confirm hook turns a recalled failure
            # into a non_goal, which is frozen onto the new contract.
            seen: dict[str, int] = {}

            def confirm(candidates):
                seen["count"] = len(candidates)
                return ["avoid the empty-input parser path that failed before"]

            run2 = engine.run_task(
                objective="parse the grammar again",
                work=_evidence_work(engine),
                verifiers=_passing_verifiers(),
                verification=["pytest"],
                stop_condition={"max_iterations": 3},
                failure_scope="parser-tasks",
                confirm_guardrails=confirm,
            )
            self.assertGreaterEqual(seen["count"], 1)
            self.assertIn(
                "avoid the empty-input parser path that failed before", run2.task.non_goals
            )
            self.assertTrue(run2.passed)

    def test_without_confirm_hook_nothing_is_injected(self) -> None:
        # A scope with known failures still injects nothing without a confirm
        # hook: guardrails must not auto-ratchet (ADR 0005 §2.5).
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            engine.memory.record_failure("a known parser failure", scope="parser-tasks")

            run = engine.run_task(
                objective="parse the grammar",
                work=_evidence_work(engine),
                verifiers=_passing_verifiers(),
                verification=["pytest"],
                stop_condition={"max_iterations": 3},
                non_goals=["base non-goal"],
                failure_scope="parser-tasks",
            )
            self.assertEqual(run.task.non_goals, ("base non-goal",))

    def test_expired_failures_are_not_offered(self) -> None:
        # active_only enforces the TTL loop: an expired failure in the scope is
        # never surfaced to the confirm hook.
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            engine.memory.record_failure("a fresh failure", scope="parser-tasks")
            # Backdate an entry past its TTL by writing it straight to the log.
            expired = MemoryEntry(
                type="failures",
                summary="an ancient failure",
                metadata={"scope": "parser-tasks", "ttl_days": 1},
                created_at="2020-01-01T00:00:00+00:00",
            )
            DurableJsonl(engine.memory._log_path("failures")).append(
                json.dumps(expired.to_dict(), ensure_ascii=True)
            )

            offered: dict[str, list[str]] = {}

            def confirm(candidates):
                offered["summaries"] = [c.summary for c in candidates]
                return []

            engine.run_task(
                objective="parse the grammar",
                work=_evidence_work(engine),
                verifiers=_passing_verifiers(),
                verification=["pytest"],
                stop_condition={"max_iterations": 3},
                failure_scope="parser-tasks",
                confirm_guardrails=confirm,
            )
            self.assertIn("a fresh failure", offered["summaries"])
            self.assertNotIn("an ancient failure", offered["summaries"])

    def test_confirmed_guardrail_is_deduped_against_base_non_goals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            engine.memory.record_failure("dup failure", scope="parser-tasks")

            run = engine.run_task(
                objective="parse the grammar",
                work=_evidence_work(engine),
                verifiers=_passing_verifiers(),
                verification=["pytest"],
                stop_condition={"max_iterations": 3},
                non_goals=["do not touch the lexer"],
                failure_scope="parser-tasks",
                confirm_guardrails=lambda candidates: ["do not touch the lexer"],
            )
            self.assertEqual(run.task.non_goals.count("do not touch the lexer"), 1)

    def test_default_scope_unchanged_without_failure_scope(self) -> None:
        # Regression guard: omitting failure_scope keeps the per-contract scope.
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            run = engine.run_task(
                objective="parse the grammar",
                work=_evidence_work(engine),
                verifiers=_failing_verifiers(),
                verification=["pytest"],
                stop_condition={"max_iterations": 1, "no_progress_iterations": 99},
            )
            failures = engine.memory.entries("failures")
            self.assertTrue(failures)
            self.assertTrue(
                all(e.metadata.get("scope") == f"contract:{run.task.goal_id}" for e in failures)
            )


if __name__ == "__main__":
    unittest.main()
