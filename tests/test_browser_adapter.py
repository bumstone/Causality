from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Mapping, Sequence

from causality.browser_adapter import (
    A11yBrowserAdapter,
    BrowserAction,
    BrowserContext,
    BrowserInputLimitError,
    BrowserOutputLimitError,
    CommandResult,
    compression_stats,
    wrap_untrusted,
)


CAPABILITIES = {
    "protocol_version": 1,
    "session_isolation": True,
    "network_scope_enforcement": True,
    "operations": [
        "observe",
        "act",
        "assert",
        "inspect",
        "visual",
        "console",
        "network",
    ],
}


class BrowserAdapterTests(unittest.TestCase):
    def test_capability_handshake_accepts_command_prefix(self) -> None:
        calls: list[tuple[list[str], dict[str, str]]] = []

        def runner(
            command: Sequence[str], environment: Mapping[str, str]
        ) -> CommandResult:
            calls.append((list(command), dict(environment)))
            return CommandResult(0, json.dumps(CAPABILITIES), "")

        adapter = A11yBrowserAdapter(("python", "driver.py"), runner=runner)

        self.assertEqual(adapter.capabilities().protocol_version, 1)
        self.assertEqual(calls[0][0], ["python", "driver.py", "capabilities", "--json"])

    def test_observe_uses_stable_scope_and_sanitized_session_environment(self) -> None:
        calls: list[tuple[list[str], dict[str, str]]] = []

        def runner(
            command: Sequence[str], environment: Mapping[str, str]
        ) -> CommandResult:
            calls.append((list(command), dict(environment)))
            return CommandResult(0, '@e1 [button] "Submit"\n@e2 [textbox] "Email"', "")

        previous = os.environ.get("CAUSALITY_APPROVAL_TOKEN")
        os.environ["CAUSALITY_APPROVAL_TOKEN"] = "must-not-leak"
        try:
            adapter = A11yBrowserAdapter(("python", "driver.py"), runner=runner)
            observation = adapter.observe(
                "interactive",
                scope="@e2",
                diff=True,
                context=BrowserContext(
                    "opaque-session",
                    "profile-dir",
                    ("https://example.com",),
                ),
            )
        finally:
            if previous is None:
                os.environ.pop("CAUSALITY_APPROVAL_TOKEN", None)
            else:
                os.environ["CAUSALITY_APPROVAL_TOKEN"] = previous

        command, environment = calls[0]
        self.assertEqual(
            command,
            ["python", "driver.py", "snapshot", "-i", "-s", "@e2", "-D"],
        )
        self.assertNotIn("CAUSALITY_APPROVAL_TOKEN", environment)
        self.assertEqual(environment["CAUSALITY_BROWSER_SESSION_ID"], "opaque-session")
        self.assertEqual(environment["CAUSALITY_BROWSER_PROFILE_DIR"], "profile-dir")
        self.assertEqual(
            json.loads(environment["CAUSALITY_BROWSER_ALLOWED_ORIGINS_JSON"]),
            ["https://example.com"],
        )
        self.assertEqual(observation.ref_count, 2)
        self.assertIn("UNTRUSTED", observation.untrusted_snapshot)

    def test_observe_rejects_non_ref_scope(self) -> None:
        adapter = A11yBrowserAdapter(
            "browse",
            runner=lambda _command, _environment: CommandResult(0, "", ""),
        )

        with self.assertRaises(ValueError):
            adapter.observe(scope="#invented-selector")

    def test_act_rejects_invalid_ref_and_runs_ref_command(self) -> None:
        commands: list[list[str]] = []

        def runner(
            command: Sequence[str], _environment: Mapping[str, str]
        ) -> CommandResult:
            commands.append(list(command))
            return CommandResult(0, "ok", "")

        adapter = A11yBrowserAdapter("browse", runner=runner)
        with self.assertRaises(ValueError):
            adapter.act(BrowserAction("#made-up", "click"))

        adapter.act(BrowserAction("@e3", "fill", "hello@example.com"))
        self.assertEqual(commands[0], ["browse", "fill", "@e3", "hello@example.com"])

    def test_assert_and_inspect_accept_only_stable_refs(self) -> None:
        commands: list[list[str]] = []

        def runner(
            command: Sequence[str], _environment: Mapping[str, str]
        ) -> CommandResult:
            commands.append(list(command))
            return CommandResult(0, "value", "")

        adapter = A11yBrowserAdapter("browse", runner=runner)
        adapter.assert_state("visible", "@e1")
        adapter.inspect("@e1", "attrs")

        self.assertEqual(commands, [["browse", "is", "visible", "@e1"], ["browse", "attrs", "@e1"]])
        with self.assertRaises(ValueError):
            adapter.assert_state("visible", ".selector")
        with self.assertRaises(ValueError):
            adapter.inspect(".selector")

    def test_diagnostics_collect_console_and_network_deltas(self) -> None:
        def runner(
            command: Sequence[str], _environment: Mapping[str, str]
        ) -> CommandResult:
            return CommandResult(0, command[-1] + " delta", "")

        adapter = A11yBrowserAdapter("browse", runner=runner)
        deltas = adapter.diagnostics()

        self.assertEqual(deltas.console, "console delta")
        self.assertEqual(deltas.network, "network delta")
        self.assertEqual(deltas.console_sha256, hashlib.sha256(b"console delta").hexdigest())

    def test_output_limit_fails_without_echoing_page_content(self) -> None:
        adapter = A11yBrowserAdapter(
            "browse",
            max_output_bytes=4,
            runner=lambda _command, _environment: CommandResult(0, "secret page", ""),
        )

        with self.assertRaises(BrowserOutputLimitError) as caught:
            adapter.observe()

        self.assertNotIn("secret page", str(caught.exception))

    def test_action_input_limit_fails_before_driver(self) -> None:
        calls: list[list[str]] = []

        def runner(
            command: Sequence[str], _environment: Mapping[str, str]
        ) -> CommandResult:
            calls.append(list(command))
            return CommandResult(0, "", "")

        adapter = A11yBrowserAdapter(
            "browse", max_action_value_bytes=4, runner=runner
        )

        with self.assertRaises(BrowserInputLimitError):
            adapter.act(BrowserAction("@e1", "fill", "secret"))
        self.assertEqual(calls, [])

    def test_visual_atomically_replaces_hardlink_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "outside.png"
            target = root / "visual.png"
            source.write_bytes(b"original")
            os.link(source, target)

            def runner(
                command: Sequence[str], _environment: Mapping[str, str]
            ) -> CommandResult:
                Path(command[-1]).write_bytes(b"new-image")
                return CommandResult(0, "", "")

            adapter = A11yBrowserAdapter("browse", runner=runner)
            artifact = adapter.visual(target, target_ref="@e1")

            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(target.read_bytes(), b"new-image")
            self.assertEqual(artifact.bytes, len(b"new-image"))
            self.assertEqual(artifact.sha256, hashlib.sha256(b"new-image").hexdigest())

    def test_compression_stats(self) -> None:
        stats = compression_stats("line\n" * 100, "@e1 [button]\n")

        self.assertLess(stats["char_compression_ratio"], 1.0)
        self.assertIn("UNTRUSTED", wrap_untrusted("page text"))


if __name__ == "__main__":
    unittest.main()
