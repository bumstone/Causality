from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.cli import main
from causality.contracts import AuditEventType
from causality.ledger import EvidenceLedger


class CLITests(unittest.TestCase):
    def test_context_omits_raw_payload_artifacts_and_contract_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / "ledger.jsonl"
            artifact = root / "credential.txt"
            artifact.write_text("secret", encoding="utf-8")
            sentinel = "cli-context-secret-sentinel"
            EvidenceLedger(ledger_path).append(
                AuditEventType.EVIDENCE,
                {"token": sentinel},
                contract_id=f"contract-{sentinel}",
                artifact_paths=(artifact,),
            )

            stdout = io.StringIO()
            argv = ["causality", "context", "--ledger", str(ledger_path), "--pretty"]
            with mock.patch.object(sys, "argv", argv), redirect_stdout(stdout):
                self.assertEqual(main(), 0)

            output = stdout.getvalue()
            context = json.loads(output)
            event = context["ledger_tail"][0]
            self.assertNotIn(sentinel, output)
            self.assertNotIn(str(artifact), output)
            self.assertNotIn("payload", event)
            self.assertNotIn("contract_id", event)
            self.assertIn("contract_ref", event)

    def test_install_agent_exit_contract(self) -> None:
        for activation, expected in (("active", 0), ("pending", 0), ("broken", 1)):
            with self.subTest(activation=activation):
                result = SimpleNamespace(
                    activation=activation,
                    to_dict=lambda value=activation: {"activation": value},
                )
                output = io.StringIO()
                with (
                    mock.patch(
                        "causality.cli.install_agent_files", return_value=result
                    ) as installer,
                    mock.patch.object(
                        sys,
                        "argv",
                        [
                            "causality",
                            "install-agent",
                            "--client",
                            "generic",
                            "--adopt",
                            "--verify",
                        ],
                    ),
                    redirect_stdout(output),
                ):
                    exit_code = main()

                self.assertEqual(exit_code, expected)
                self.assertEqual(json.loads(output.getvalue())["activation"], activation)
                installer.assert_called_once_with(
                    ".",
                    force=False,
                    client="generic",
                    adopt=True,
                    verify=True,
                )


if __name__ == "__main__":
    unittest.main()
