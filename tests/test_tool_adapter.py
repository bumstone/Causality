from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.browser_adapter import CommandResult
from causality.contracts import AuditEventType, GoalContract, PermissionContract
from causality.execution import ActionBlocked, ExecutionAdapter
from causality.orchestrator import Causality
from causality.tool_adapter import ToolAdapter


def _recording_runner(record: list, result: CommandResult = CommandResult(0, "", "")):
    def run(argv):
        record.append(list(argv))
        return result
    return run


class ToolAdapterTests(unittest.TestCase):
    def _setup(self, temp_dir: str, **contract_kw):
        runtime = Causality(Path(temp_dir) / "ledger.jsonl")
        contract = runtime.create_contract(GoalContract(title="t", summary="s", **contract_kw))
        adapter = ExecutionAdapter(runtime, contract)
        return runtime, contract, adapter

    def test_run_gates_records_and_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, contract, adapter = self._setup(temp_dir)
            calls: list = []
            tool = ToolAdapter(
                runtime.ledger,
                adapter,
                runner=_recording_runner(calls, CommandResult(0, "hello\n", "")),
            )
            result = tool.run(["echo", "hello"])

            self.assertEqual(calls, [["echo", "hello"]])  # list args -> no shell
            self.assertEqual((result.exit_code, result.stdout), (0, "hello\n"))
            tool_calls = runtime.ledger.find(AuditEventType.TOOL_CALL)
            self.assertEqual(len(tool_calls), 1)
            self.assertEqual(tool_calls[0].payload["argv"], ["echo", "hello"])
            self.assertEqual(tool_calls[0].contract_id, contract.goal_id)

    def test_run_blocked_by_non_goal_neither_executes_nor_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _, adapter = self._setup(temp_dir, non_goals=("delete production",))
            calls: list = []
            tool = ToolAdapter(runtime.ledger, adapter, runner=_recording_runner(calls))
            with self.assertRaises(ActionBlocked):
                tool.run(["rm", "-rf", "/data"], description="delete production database")
            self.assertEqual(calls, [])  # the command never ran
            self.assertEqual(runtime.ledger.find(AuditEventType.TOOL_CALL), [])  # not recorded

    def test_run_blocked_when_tool_not_in_allowed_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _, adapter = self._setup(
                temp_dir, permissions=PermissionContract(allowed_tools=("git",))
            )
            tool = ToolAdapter(runtime.ledger, adapter, runner=_recording_runner([]))
            with self.assertRaises(ActionBlocked):
                tool.run(["echo", "hi"], tool="shell")  # "shell" not allowed

    def test_run_rejects_empty_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _, adapter = self._setup(temp_dir)
            tool = ToolAdapter(runtime.ledger, adapter, runner=_recording_runner([]))
            with self.assertRaises(ValueError):
                tool.run([])

    def test_write_then_read_text_with_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _, adapter = self._setup(temp_dir)
            tool = ToolAdapter(runtime.ledger, adapter)  # real file ops, no subprocess
            target = Path(temp_dir) / "out" / "note.txt"

            written = tool.write_text(target, "hello world")
            self.assertEqual(written, target)
            self.assertEqual(target.read_text(encoding="utf-8"), "hello world")
            evidence = runtime.ledger.find(AuditEventType.EVIDENCE)
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0].payload["path"], str(target))
            self.assertTrue(evidence[0].artifacts and evidence[0].artifacts[0]["sha256"])

            content = tool.read_text(target)
            self.assertEqual(content, "hello world")
            reads = [
                e for e in runtime.ledger.find(AuditEventType.TOOL_CALL)
                if e.payload.get("tool") == "file.read"
            ]
            self.assertEqual(len(reads), 1)


if __name__ == "__main__":
    unittest.main()
