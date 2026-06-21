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

    def test_run_executes_in_adapter_root(self) -> None:
        # codex r3448164499: a real subprocess runs with cwd=root, so a relative
        # command operates on the same tree as file ops, not the ambient cwd.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            runtime, _, adapter = self._setup(temp_dir)
            tool = ToolAdapter(runtime.ledger, adapter, root=workspace)  # real subprocess
            result = tool.run([sys.executable, "-c", "import os; print(os.getcwd())"])
            self.assertEqual(Path(result.stdout.strip()), workspace.resolve())

    def test_run_rejects_empty_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _, adapter = self._setup(temp_dir)
            tool = ToolAdapter(runtime.ledger, adapter, runner=_recording_runner([]))
            with self.assertRaises(ValueError):
                tool.run([])

    def test_write_blocked_outside_write_scope(self) -> None:
        # codex r3448136006: write_text must honor the contract's frozen file
        # boundary (write_scope), not just the generic tool/risk/non-goal gates.
        with tempfile.TemporaryDirectory() as temp_dir:
            allowed = Path(temp_dir) / "workspace"
            runtime, _, adapter = self._setup(
                temp_dir, permissions=PermissionContract(write_scope=(str(allowed),))
            )
            tool = ToolAdapter(runtime.ledger, adapter)

            # In-scope write succeeds.
            written = tool.write_text(allowed / "src" / "ok.txt", "fine")
            self.assertTrue(written.exists())

            # Out-of-scope write is blocked, the file is never created, and a STOP
            # gate decision is recorded for audit.
            outside = Path(temp_dir) / "elsewhere" / "bad.txt"
            with self.assertRaises(ActionBlocked):
                tool.write_text(outside, "nope")
            self.assertFalse(outside.exists())
            gate_decisions = runtime.ledger.find(AuditEventType.GATE_DECISION)
            self.assertTrue(any(g.payload.get("decision") == "stop" for g in gate_decisions))

    def test_relative_write_scope_resolves_against_root(self) -> None:
        # codex r3448146018: a relative write_scope ("workspace") and relative
        # targets must resolve against the adapter's root, not the process cwd.
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _, adapter = self._setup(
                temp_dir, permissions=PermissionContract(write_scope=("workspace",))
            )
            tool = ToolAdapter(runtime.ledger, adapter, root=Path(temp_dir))

            written = tool.write_text("workspace/ok.txt", "fine")  # in-scope, root-anchored
            self.assertEqual(written, (Path(temp_dir) / "workspace" / "ok.txt").resolve())
            self.assertTrue(written.exists())

            with self.assertRaises(ActionBlocked):
                tool.write_text("elsewhere/bad.txt", "nope")
            self.assertFalse((Path(temp_dir) / "elsewhere" / "bad.txt").exists())

    def test_relative_root_resolved_at_construction_not_write_time(self) -> None:
        # codex r3448157732: a relative root must be resolved when the adapter is
        # built, so a later cwd change can't move the scope/target tree.
        import os

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _, adapter = self._setup(temp_dir)
            cwd = os.getcwd()
            try:
                (Path(temp_dir) / "repo").mkdir()
                os.chdir(temp_dir)
                tool = ToolAdapter(runtime.ledger, adapter, root=Path("repo"))  # relative
                self.assertEqual(tool.root, (Path(temp_dir) / "repo").resolve())
                os.chdir(cwd)  # moving cwd afterwards must not move the root
                written = tool.write_text("note.txt", "x")
                self.assertEqual(written, (Path(temp_dir) / "repo" / "note.txt").resolve())
                self.assertTrue(written.exists())
            finally:
                os.chdir(cwd)

    def test_write_then_read_text_with_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime, _, adapter = self._setup(temp_dir)
            tool = ToolAdapter(runtime.ledger, adapter)  # real file ops, no subprocess
            target = Path(temp_dir) / "out" / "note.txt"

            written = tool.write_text(target, "hello world")
            self.assertTrue(written.exists())
            self.assertEqual(written.read_text(encoding="utf-8"), "hello world")
            evidence = runtime.ledger.find(AuditEventType.EVIDENCE)
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0].payload["path"], str(written))
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
