from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.ledger import EvidenceLedger
import test_mcp_external as _external_support

REPO_ROOT = _external_support.REPO_ROOT
SRC_ROOT = _external_support.SRC_ROOT
_tree_snapshot = _external_support._tree_snapshot


ORIGIN = "https://browser.example"
PAGE_SECRET = "external-page-secret-004b"
FILL_SECRET = "external-fill-secret-004b"
CONSOLE_SECRET = "external-console-secret-004b"
NETWORK_SECRET = "external-network-secret-004b"
APPROVAL_TOKEN = "external-approval-secret-004b"
BROWSER_TOOLS = [
    "browser.act",
    "browser.assert",
    "browser.inspect",
    "browser.observe",
    "browser.visual",
]


FAKE_DRIVER_SOURCE = r'''
import hashlib
import json
import os
import sys
from pathlib import Path

ORIGIN = __ORIGIN__
PAGE_SECRET = __PAGE_SECRET__
CONSOLE_SECRET = __CONSOLE_SECRET__
NETWORK_SECRET = __NETWORK_SECRET__

state_path = Path(sys.argv[1])
operation = sys.argv[2]
args = sys.argv[3:]

if operation == "capabilities":
    print(json.dumps({
        "protocol_version": 1,
        "session_isolation": True,
        "network_scope_enforcement": True,
        "operations": ["observe", "act", "assert", "inspect", "visual", "console", "network"],
    }))
    raise SystemExit(0)

allowed_keys = {
    "CAUSALITY_BROWSER_SESSION_ID",
    "CAUSALITY_BROWSER_PROFILE_DIR",
    "CAUSALITY_BROWSER_ALLOWED_ORIGINS_JSON",
}
if any(name.startswith("CAUSALITY_") and name not in allowed_keys for name in os.environ):
    raise SystemExit(13)
if "AWS_SECRET_ACCESS_KEY" in os.environ:
    raise SystemExit(16)
session = os.environ["CAUSALITY_BROWSER_SESSION_ID"]
profile = Path(os.environ["CAUSALITY_BROWSER_PROFILE_DIR"])
origins = json.loads(os.environ["CAUSALITY_BROWSER_ALLOWED_ORIGINS_JSON"])
if not profile.is_dir() or origins != [ORIGIN]:
    raise SystemExit(14)

state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"sessions": {}}
current = state["sessions"].setdefault(
    session,
    {"phase": "before", "effects": 0, "visuals": 0, "value_sha256": None},
)

def snapshot():
    phase = current["phase"]
    return f'@e1 [button] "{phase} {PAGE_SECRET}"\n@e2 [textbox] "Email"'

if operation == "snapshot":
    print(snapshot())
elif operation in {"click", "fill", "hover", "press", "select"}:
    current["effects"] += 1
    current["phase"] = "after"
    if operation in {"fill", "press", "select"}:
        if not args or args[-1] != "--value-stdin":
            raise SystemExit(17)
        value = sys.stdin.buffer.read().decode("utf-8")
        current["value_sha256"] = hashlib.sha256(value.encode()).hexdigest()
    print("acted")
elif operation == "console":
    print(CONSOLE_SECRET if current["effects"] else "")
elif operation == "network":
    print(NETWORK_SECRET if current["effects"] else "")
elif operation == "is":
    print("true")
elif operation in {"attrs", "html", "css"}:
    print(f"inspection {PAGE_SECRET}")
elif operation == "screenshot":
    Path(args[-1]).write_bytes(b"external-fake-png")
    current["visuals"] += 1
else:
    raise SystemExit(15)

temporary = state_path.with_suffix(".tmp")
temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
os.replace(temporary, state_path)
'''


class ExternalBrowserMCPTests(unittest.TestCase):
    _request = staticmethod(_external_support.ExternalMCPTests._request)
    _exchange = _external_support.ExternalMCPTests._exchange
    _finish_server = _external_support.ExternalMCPTests._finish_server
    _server = _external_support.ExternalMCPTests._server

    def test_installed_browser_lifecycle_is_isolated_and_exactly_once(self) -> None:
        repo_build = REPO_ROOT / "build"
        build_before = _tree_snapshot(repo_build)
        self.addCleanup(
            lambda: self.assertEqual(
                _tree_snapshot(repo_build),
                build_before,
                "external package installation polluted the repository build tree",
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            environment = base / "fresh venv"
            project = base / "external project"
            package_source = base / "package source"
            driver = base / "fake_browser_driver.py"
            driver_state = base / "fake_browser_state.json"
            project.mkdir()
            package_source.mkdir()
            driver.write_text(
                FAKE_DRIVER_SOURCE.replace("__ORIGIN__", repr(ORIGIN))
                .replace("__PAGE_SECRET__", repr(PAGE_SECRET))
                .replace("__CONSOLE_SECRET__", repr(CONSOLE_SECRET))
                .replace("__NETWORK_SECRET__", repr(NETWORK_SECRET)),
                encoding="utf-8",
            )
            (project / "test_browser_acceptance.py").write_text(
                "import os\n"
                "import unittest\n"
                "from pathlib import Path\n\n"
                "class BrowserAcceptance(unittest.TestCase):\n"
                "    def test_visual_and_secret_isolation(self):\n"
                "        self.assertFalse(any(name.startswith('CAUSALITY_') for name in os.environ))\n"
                "        artifacts = list(Path('.causality/browser/artifacts').rglob('*.png'))\n"
                "        self.assertEqual(len(artifacts), 1)\n"
                "        self.assertEqual(artifacts[0].read_bytes(), b'external-fake-png')\n",
                encoding="utf-8",
            )
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy2(REPO_ROOT / name, package_source / name)
            shutil.copytree(
                SRC_ROOT,
                package_source / "src",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

            venv.EnvBuilder(with_pip=True).create(environment)
            python = environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            clean_env = {
                name: value
                for name, value in os.environ.items()
                if not name.startswith("CAUSALITY_") and name != "PYTHONPATH"
            }
            clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
            installed = subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--no-deps",
                    str(package_source),
                ],
                cwd=project,
                env=clean_env,
                text=True,
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            imported = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import causality, pathlib; print(pathlib.Path(causality.__file__).resolve())",
                ],
                cwd=project,
                env=clean_env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertNotIn(str(REPO_ROOT), imported.stdout)

            verification_argv = [
                str(python),
                "-m",
                "unittest",
                "discover",
                "-s",
                ".",
                "-p",
                "test_browser_acceptance.py",
                "-v",
            ]
            server_env = dict(clean_env)
            server_env["AWS_SECRET_ACCESS_KEY"] = "must-not-reach-browser"
            server_env.update(
                {
                    "CAUSALITY_BROWSER_COMMAND_JSON": json.dumps(
                        [str(python), str(driver), str(driver_state)]
                    ),
                    "CAUSALITY_NETWORK_ORIGINS_JSON": json.dumps([ORIGIN]),
                    "CAUSALITY_VERIFICATION_COMMANDS_JSON": json.dumps(
                        [verification_argv]
                    ),
                    "CAUSALITY_APPROVAL_TOKEN": APPROVAL_TOKEN,
                }
            )

            request_id = 1
            begin = {
                "objective": "exercise an installed isolated browser lifecycle",
                "risk": "low",
                "permissions": {
                    "allowed_tools": [*BROWSER_TOOLS, "shell"],
                    "write_scope": [],
                    "network_scope": [ORIGIN],
                    "auth_scope": [],
                },
                "verification_requirements": [
                    {
                        "id": "browser-acceptance",
                        "argv": verification_argv,
                        "expected_exit_codes": [0],
                        "timeout_seconds": 60,
                        "artifact_paths": {},
                        "required": True,
                        "manual": False,
                    }
                ],
                "evidence_required": [
                    {
                        "kind": kind,
                        "description": description,
                        "required": True,
                    }
                    for kind, description in (
                        ("browser_diff", "state-bound before/after diff"),
                        ("a11y_report", "post-action accessibility state"),
                        ("artifact_hash", "visual artifact hash"),
                    )
                ],
                "stop_condition": {
                    "max_iterations": 8,
                    "max_failed_hypotheses": 3,
                    "no_progress_iterations": 2,
                },
                "non_goals": ["reuse a personal browser profile"],
                "idempotency_key": "external-browser-begin",
            }

            with self._server(python, project, server_env) as process:
                begun = self._exchange(
                    process,
                    self._request(request_id, "causality_task_begin", begin),
                )
                request_id += 1
                task_id = begun["task"]["task_id"]
                observed = self._exchange(
                    process,
                    self._request(
                        request_id,
                        "causality_task_browser",
                        {
                            "task_id": task_id,
                            "idempotency_key": "external-browser-observe",
                            "operation": "observe",
                            "mode": "interactive",
                        },
                    ),
                )
                request_id += 1
                self.assertIn(
                    PAGE_SECRET, observed["data"]["untrusted"]["snapshot"]
                )
                approved = self._exchange(
                    process,
                    self._request(
                        request_id,
                        "causality_task_approve",
                        {
                            "task_id": task_id,
                            "idempotency_key": "external-browser-approval",
                            "stage": "external_send",
                            "approved": True,
                            "approver": "operator",
                            "rationale": "approve one isolated DOM action",
                            "evidence_refs": [],
                            "proof": APPROVAL_TOKEN,
                        },
                    ),
                )
                request_id += 1
                self.assertEqual(approved["task"]["state"], "executing")
                act_arguments = {
                    "task_id": task_id,
                    "idempotency_key": "external-browser-act",
                    "operation": "act",
                    "action": "fill",
                    "ref": "@e2",
                    "value": FILL_SECRET,
                    "expected_state_hash": observed["data"]["state_hash"],
                }
                acted = self._exchange(
                    process,
                    self._request(
                        request_id, "causality_task_browser", act_arguments
                    ),
                )
                request_id += 1
                after_hash = acted["data"]["after_state_hash"]
                for operation, extra in (
                    ("assert", {"property": "visible", "ref": "@e1"}),
                    ("inspect", {"inspection": "attrs", "ref": "@e1"}),
                ):
                    response = self._exchange(
                        process,
                        self._request(
                            request_id,
                            "causality_task_browser",
                            {
                                "task_id": task_id,
                                "idempotency_key": f"external-browser-{operation}",
                                "operation": operation,
                                "expected_state_hash": after_hash,
                                **extra,
                            },
                        ),
                    )
                    request_id += 1
                    if operation == "inspect":
                        self.assertIn(
                            PAGE_SECRET,
                            response["data"]["untrusted"]["inspection"],
                        )
                visual_arguments = {
                    "task_id": task_id,
                    "idempotency_key": "external-browser-visual",
                    "operation": "visual",
                    "ref": "@e1",
                    "expected_state_hash": after_hash,
                }
                visual = self._exchange(
                    process,
                    self._request(
                        request_id, "causality_task_browser", visual_arguments
                    ),
                )
                request_id += 1
                artifact = Path(visual["data"]["artifact"]["path"])
                artifact_mtime = artifact.stat().st_mtime_ns

            with self._server(python, project, server_env) as process:
                replayed_act = self._exchange(
                    process,
                    self._request(
                        request_id, "causality_task_browser", act_arguments
                    ),
                )
                request_id += 1
                self.assertTrue(replayed_act["idempotency"]["replayed"])
                self.assertEqual(replayed_act["data"], acted["data"])
                replayed_visual = self._exchange(
                    process,
                    self._request(
                        request_id, "causality_task_browser", visual_arguments
                    ),
                )
                request_id += 1
                self.assertTrue(replayed_visual["idempotency"]["replayed"])
                self.assertEqual(artifact.stat().st_mtime_ns, artifact_mtime)

                verified = self._exchange(
                    process,
                    self._request(
                        request_id,
                        "causality_task_verify",
                        {
                            "task_id": task_id,
                            "idempotency_key": "external-browser-verify",
                            "requirement_id": "browser-acceptance",
                            "mode": "execute",
                        },
                    ),
                    timeout=90,
                )
                request_id += 1
                ledger = EvidenceLedger(project / ".causality" / "ledger.jsonl")
                latest_evidence: dict[str, str] = {}
                for event in ledger.events_for_contract(task_id, all_segments=True):
                    if event.event_type == "evidence":
                        kind = event.payload.get("kind")
                        if kind in {
                            "browser_diff",
                            "a11y_report",
                            "artifact_hash",
                        }:
                            latest_evidence[kind] = event.entry_hash
                evidence_refs = [
                    latest_evidence[kind]
                    for kind in ("browser_diff", "a11y_report", "artifact_hash")
                ] + [verified["event_hash"]]
                for index in (1, 2):
                    self._exchange(
                        process,
                        self._request(
                            request_id,
                            "causality_task_verdict",
                            {
                                "task_id": task_id,
                                "idempotency_key": f"external-browser-verdict-{index}",
                                "verifier": f"external-browser-review-{index}",
                                "status": "pass",
                                "rationale": "installed browser evidence is consistent",
                                "evidence_refs": evidence_refs,
                            },
                        ),
                    )
                    request_id += 1
                completed = self._exchange(
                    process,
                    self._request(
                        request_id,
                        "causality_task_complete",
                        {
                            "task_id": task_id,
                            "idempotency_key": "external-browser-complete",
                        },
                    ),
                )
                self.assertEqual(completed["task"]["state"], "verified")

            state = json.loads(driver_state.read_text(encoding="utf-8"))
            self.assertEqual(len(state["sessions"]), 1)
            driver_session = next(iter(state["sessions"].values()))
            self.assertEqual(driver_session["effects"], 1)
            self.assertEqual(driver_session["visuals"], 1)
            self.assertEqual(
                driver_session["value_sha256"],
                hashlib.sha256(FILL_SECRET.encode()).hexdigest(),
            )
            ledger = EvidenceLedger(project / ".causality" / "ledger.jsonl")
            self.assertTrue(ledger.verify_chain())
            ledger_text = ledger.path.read_text(encoding="utf-8")
            for secret in (
                PAGE_SECRET,
                FILL_SECRET,
                CONSOLE_SECRET,
                NETWORK_SECRET,
                APPROVAL_TOKEN,
            ):
                self.assertNotIn(secret, ledger_text)


if __name__ == "__main__":
    unittest.main()
