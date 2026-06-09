from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ouroboros_hitl.browser_adapter import (
    A11yBrowserAdapter,
    BrowserAction,
    CommandResult,
    compression_stats,
    wrap_untrusted,
)
from ouroboros_hitl.ledger import EvidenceLedger


class BrowserAdapterTests(unittest.TestCase):
    def test_observe_uses_interactive_snapshot_and_logs(self) -> None:
        commands: list[list[str]] = []

        def runner(command: Sequence[str]) -> CommandResult:
            commands.append(list(command))
            return CommandResult(
                0,
                '@e1 [button] "Submit"\n@e2 [textbox] "Email"',
                "",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = EvidenceLedger(Path(temp_dir) / "ledger.jsonl")
            adapter = A11yBrowserAdapter("browse", ledger=ledger, runner=runner)
            observation = adapter.observe("interactive", diff=True)

            self.assertIn("-i", commands[0])
            self.assertIn("-D", commands[0])
            self.assertEqual(observation.ref_count, 2)
            self.assertEqual(len(ledger.events()), 1)
            self.assertIn("UNTRUSTED", observation.untrusted_snapshot)

    def test_act_rejects_invalid_ref(self) -> None:
        adapter = A11yBrowserAdapter("browse", runner=lambda command: CommandResult(0, "ok", ""))

        with self.assertRaises(ValueError):
            adapter.act(BrowserAction("#made-up", "click"))

    def test_act_runs_ref_based_command(self) -> None:
        commands: list[list[str]] = []

        def runner(command: Sequence[str]) -> CommandResult:
            commands.append(list(command))
            return CommandResult(0, "ok", "")

        adapter = A11yBrowserAdapter("browse", runner=runner)
        adapter.act(BrowserAction("@e3", "fill", "hello@example.com"))

        self.assertEqual(commands[0], ["browse", "fill", "@e3", "hello@example.com"])

    def test_compression_stats(self) -> None:
        stats = compression_stats("line\n" * 100, "@e1 [button]\n")

        self.assertLess(stats["char_compression_ratio"], 1.0)
        self.assertIn("UNTRUSTED", wrap_untrusted("page text"))


if __name__ == "__main__":
    unittest.main()
