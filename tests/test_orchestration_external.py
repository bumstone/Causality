from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = r'''
import json, os, sys
from pathlib import Path
from typing import Any, Mapping
from causality.automatic_orchestration import CheckpointStore, InProcessMCPTransport, ReferenceOrchestrator
from causality.contracts import AuditEventType
from causality.mcp_server import CausalityMCPServer
from causality.task_lifecycle import TaskPolicy

PROJECT = Path.cwd()
PASS = (sys.executable, "-I", "-c", "print('external-pass')")
FAIL = (sys.executable, "-I", "-c", "raise SystemExit(9)")

class MatrixTransport:
    def __init__(self, delegate):
        self.delegate = delegate
        self.crash = os.environ.get("CRASH_TOOL")
        self.omit = os.environ.get("OMIT_TOOL")
    def tools(self):
        return tuple(name for name in self.delegate.tools() if name != self.omit)
    def call(self, name: str, arguments: Mapping[str, Any]):
        result = self.delegate.call(name, arguments)
        if name == self.crash:
            os._exit(77)
        return result

def contract(name: str, *, failing=False, risk="low"):
    target = f"out/{name}.txt"
    return {
        "objective": f"external orchestration {name}", "risk": risk,
        "permissions": {"allowed_tools": ["file.write", "shell"],
                        "write_scope": ["out"], "network_scope": [], "auth_scope": []},
        "verification_requirements": [{
            "id": f"verify-{name}", "argv": list(FAIL if failing else PASS),
            "expected_exit_codes": [0], "timeout_seconds": 30,
            "artifact_paths": {target: None}, "required": True, "manual": False,
        }],
        "stop_condition": {"max_iterations": 8, "max_failed_hypotheses": 3,
                           "no_progress_iterations": 2},
        "non_goals": ["write outside the external project"], "workflow": "auto",
    }

def emit(value):
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    print(json.dumps(value, sort_keys=True), flush=True)

server = CausalityMCPServer(
    PROJECT, approval_token=os.environ.get("EXTERNAL_PROOF"),
    policy=TaskPolicy(verification_commands=(PASS, FAIL)),
)
driver = ReferenceOrchestrator(
    MatrixTransport(InProcessMCPTransport(server)),
    CheckpointStore(PROJECT, "external-controller"), lease_seconds=30,
)
ready = driver.bootstrap()
if ready.kind != "ready":
    emit(ready); raise SystemExit(2)
mode = sys.argv[1]
task_id = sys.argv[2] if len(sys.argv) > 2 else None

if mode == "start":
    begun = driver.begin(contract("success"))
    emit({"task_id": begun["task"]["task_id"]})
elif mode == "action":
    emit(driver.submit_host_action(task_id, {"action": {
        "kind": "file_write", "path": "out/success.txt", "content": "once\n",
    }}))
elif mode == "advance":
    emit(driver.advance(task_id))
elif mode == "first-verdict":
    handoff = driver.advance(task_id)
    emit(driver.submit_verifier(
        task_id, verifier_id="code", provider_id="provider-a", status="pass",
        rationale="external code review", evidence_refs=tuple(handoff.details["evidence_refs"]),
    ))
elif mode == "second-verdict":
    handoff = driver.advance(task_id)
    refs = tuple(handoff.details["evidence_refs"])
    replay = driver.submit_verifier(
        task_id, verifier_id="code", provider_id="provider-a", status="pass",
        rationale="external code review", evidence_refs=refs,
    )
    handoff = driver.advance(task_id)
    emit({"replay": replay.kind, "second": driver.submit_verifier(
        task_id, verifier_id="security", provider_id="provider-b", status="pass",
        rationale="external security review",
        evidence_refs=tuple(handoff.details["evidence_refs"]),
    ).kind})
elif mode == "failure":
    begun = driver.begin(contract("failure", failing=True))
    task_id = begun["task"]["task_id"]
    driver.submit_host_action(task_id, {"action": {
        "kind": "file_write", "path": "out/failure.txt", "content": "failed\n",
    }})
    directive = driver.advance(task_id)
    emit({"kind": directive.kind, "state": server.lifecycle.get(task_id).state.value,
          "chain": server.ledger.verify_chain()})
elif mode == "failure-resume":
    directive = driver.advance(task_id)
    evidence = [event for event in server.ledger.events_for_contract(task_id, all_segments=True)
                if event.event_type == AuditEventType.EVIDENCE.value
                and event.payload.get("requirement_id") == "verify-failure"]
    emit({"kind": directive.kind, "evidence_count": len(evidence),
          "chain": server.ledger.verify_chain()})
elif mode == "capability":
    begun = driver.begin(contract("capability"))
    task_id = begun["task"]["task_id"]
    driver.submit_host_action(task_id, {"action": {
        "kind": "file_write", "path": "out/capability.txt", "content": "gated\n",
    }})
    emit(driver.advance(task_id))
elif mode == "reject":
    proof = os.environ["EXTERNAL_PROOF"]
    candidates = PROJECT / "skills" / "candidates.jsonl"
    candidate_count = len(candidates.read_text(encoding="utf-8").splitlines()) if candidates.exists() else 0
    begun = driver.begin(contract("rejected", risk="high"))
    task_id = begun["task"]["task_id"]
    waiting = driver.step(task_id)
    decision = driver.submit_human(task_id, {
        "stage": "plan", "approved": False, "approver": "operator",
        "rationale": "reject external plan", "evidence_refs": [],
    }, proof=proof)
    terminal = driver.advance(task_id)
    persisted = server.ledger.path.read_text(encoding="utf-8") + driver.checkpoints.path.read_text(encoding="utf-8")
    emit({"waiting": waiting.kind, "decision": decision.kind, "terminal": terminal.kind,
          "state": server.lifecycle.get(task_id).state.value,
          "reflection": server.lifecycle.get(task_id).reflection is not None,
          "proof_persisted": proof in persisted,
          "candidate_created": candidates.exists() and
              len(candidates.read_text(encoding="utf-8").splitlines()) > candidate_count})
elif mode == "inspect":
    emit({"chain": server.ledger.verify_chain(),
          "lease": server.controllers.state(task_id)})
else:
    raise SystemExit(f"unknown mode: {mode}")
'''


class ExternalOrchestrationTests(unittest.TestCase):
    def test_installed_bootstrap_driver_crash_and_stop_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            environment, project, source = root / "venv", root / "project", root / "source"
            project.mkdir(); source.mkdir()
            home, appdata, localappdata = root / "home", root / "appdata", root / "localappdata"
            for directory in (home, appdata, localappdata):
                directory.mkdir()
            canary_name = "CAUSALITY_EXTERNAL_PARENT_CANARY"
            previous_canary = os.environ.get(canary_name)
            os.environ[canary_name] = "must-not-reach-external-process"
            self.addCleanup(
                lambda: os.environ.pop(canary_name, None)
                if previous_canary is None
                else os.environ.__setitem__(canary_name, previous_canary)
            )
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy2(REPO_ROOT / name, source / name)
            shutil.copytree(REPO_ROOT / "src", source / "src",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            venv.EnvBuilder(with_pip=False).create(environment)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            safe_names = {
                "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
                "TMPDIR", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
                "SSL_CERT_FILE", "SSL_CERT_DIR",
            }
            clean_env = {name: value for name, value in os.environ.items()
                         if name.upper() in safe_names}
            clean_env.update({
                "HOME": str(home), "USERPROFILE": str(home),
                "APPDATA": str(appdata), "LOCALAPPDATA": str(localappdata),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
            })
            self.assertNotIn(canary_name, clean_env)
            located = subprocess.run(
                [str(python), "-I", "-c",
                 "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                cwd=project, env=clean_env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30)
            self.assertEqual(located.returncode, 0, located.stderr)
            site_packages = Path(located.stdout.strip())
            shutil.copytree(source / "src" / "causality", site_packages / "causality")
            dist_info = site_packages / "causality-0.1.0.dist-info"
            dist_info.mkdir()
            (dist_info / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: causality\nVersion: 0.1.0\n",
                encoding="utf-8",
            )
            (project / "causality.py").write_text(
                "raise RuntimeError('project shadow imported')\n", encoding="utf-8"
            )
            imported = subprocess.run(
                [str(python), "-I", "-c", "from pathlib import Path; import causality; "
                 f"import os; assert {canary_name!r} not in os.environ; "
                 "print(Path(causality.__file__).resolve())"],
                cwd=project, env=clean_env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30)
            self.assertEqual(imported.returncode, 0, imported.stderr)
            import_path = Path(imported.stdout.strip())
            self.assertTrue(import_path.is_relative_to(environment.resolve()))
            self.assertFalse(import_path.is_relative_to(source.resolve()))
            self.assertFalse(import_path.is_relative_to(REPO_ROOT.resolve()))
            bootstrap = subprocess.run(
                [str(python), "-I", "-m", "causality.cli", "install-agent", "--project",
                 str(project), "--client", "generic", "--verify"], cwd=project,
                env=clean_env, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60)
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)
            report = json.loads((project / ".causality" / "install-report.json").read_text())
            self.assertEqual(report["activation"], "active")
            runner = project / "matrix_runner.py"
            runner.write_text(RUNNER, encoding="utf-8")

            def run(mode: str, task_id: str | None = None, *, crash: str | None = None,
                    omit: str | None = None, proof: str | None = None,
                    expected: int = 0) -> dict:
                env = dict(clean_env)
                if crash: env["CRASH_TOOL"] = crash
                if omit: env["OMIT_TOOL"] = omit
                if proof: env["EXTERNAL_PROOF"] = proof
                command = [str(python), "-I", str(runner), mode]
                if task_id: command.append(task_id)
                completed = subprocess.run(command, cwd=project, env=env,
                                           capture_output=True, text=True, encoding="utf-8",
                                           errors="replace", timeout=90)
                self.assertEqual(completed.returncode, expected, completed.stderr)
                return json.loads(completed.stdout.strip()) if completed.stdout.strip() else {}

            run("start", crash="causality_task_begin", expected=77)
            run("start", crash="causality_task_lease", expected=77)
            checkpoint = next((project / ".causality" / "orchestration").glob("*.json"))
            task_id = json.loads(checkpoint.read_text())["task_id"]
            run("action", task_id, crash="causality_task_action", expected=77)
            run("advance", task_id, crash="causality_task_verify", expected=77)
            run("first-verdict", task_id, crash="causality_task_verdict", expected=77)
            verdicts = run("second-verdict", task_id)
            self.assertEqual(verdicts, {"replay": "advanced", "second": "advanced"})
            for tool in ("causality_task_complete", "causality_task_reflect",
                         "causality_task_lease"):
                run("advance", task_id, crash=tool, expected=77)
            terminal = run("advance", task_id)
            self.assertEqual(terminal["kind"], "terminal")
            inspection = run("inspect", task_id)
            self.assertTrue(inspection["chain"])
            self.assertEqual(inspection["lease"]["status"], "released")
            self.assertEqual((project / "out" / "success.txt").read_text(), "once\n")
            events = []
            for ledger in (project / ".causality").glob("ledger.jsonl*"):
                suffix = ledger.name.removeprefix("ledger.jsonl.").split(".", 1)[0]
                if ledger.name != "ledger.jsonl" and not suffix.isdigit():
                    continue
                events.extend(json.loads(line) for line in ledger.read_text().splitlines())
            scoped = [event for event in events if event.get("contract_id") == task_id]
            self.assertEqual(sum(
                event["event_type"] == "task_action_result"
                and event["payload"].get("operation") == "action"
                for event in scoped
            ), 1)
            self.assertEqual(sum(event["event_type"] == "verifier_decision"
                                 for event in scoped), 2)
            self.assertEqual(sum(event["event_type"] == "task_reflected"
                                 for event in scoped), 1)
            self.assertEqual(sum(
                event["event_type"] == "task_operation"
                and event["payload"].get("operation") == "complete"
                for event in scoped
            ), 1)
            controller = [
                event for event in events
                if event.get("contract_id") == f"controller:{task_id}"
            ]
            self.assertEqual(sum(
                event["event_type"] == "task_controller_lease"
                and event["payload"].get("action") == "release"
                for event in controller
            ), 1)

            run("failure", crash="causality_task_verify", expected=77)
            failed_task = json.loads(checkpoint.read_text())["task_id"]
            failure = run("failure-resume", failed_task)
            self.assertEqual(failure, {"chain": True, "evidence_count": 1,
                                       "kind": "verification_failed"})
            gated = run("capability", omit="causality_task_verify")
            self.assertEqual(gated["kind"], "capability_unavailable")
            proof = "external-proof-must-not-persist"
            rejected = run("reject", proof=proof)
            self.assertEqual(rejected["state"], "rejected")
            self.assertEqual(rejected["terminal"], "terminal")
            self.assertTrue(rejected["reflection"])
            self.assertFalse(rejected["proof_persisted"])
            self.assertFalse(rejected["candidate_created"])
            for path in project.rglob("*"):
                if path.is_file():
                    self.assertNotIn(proof.encode(), path.read_bytes(), str(path))


if __name__ == "__main__":
    unittest.main()
