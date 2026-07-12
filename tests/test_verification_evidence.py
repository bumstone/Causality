from __future__ import annotations

import hashlib
import os
import py_compile
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import warnings
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.browser_adapter import CommandResult
import causality.ledger as ledger_module
from causality.contract_harness import ContractHarness
from causality.contracts import (
    AuditEventType,
    EvidenceKind,
    EvidenceRequirement,
    GateDecision,
    GoalContract,
    PermissionContract,
    Risk,
    StateTransition,
    VerificationRequirement,
    VerifierDecision,
)
from causality.orchestrator import Causality
from causality.execution import ExecutionAdapter
from causality.tool_adapter import ToolAdapter


def _python(*source_and_args: str) -> tuple[str, ...]:
    return (sys.executable, "-c", *source_and_args)


def _requirement(
    requirement_id: str = "unit",
    *,
    argv: tuple[str, ...] | None = None,
    expected_exit_codes: tuple[int, ...] = (0,),
    timeout_seconds: float = 5.0,
    artifact_paths: dict[str, str | None] | None = None,
    manual: bool = False,
    required: bool = True,
) -> VerificationRequirement:
    return VerificationRequirement(
        id=requirement_id,
        argv=() if manual else (argv or _python("print('ok')")),
        expected_exit_codes=expected_exit_codes,
        timeout_seconds=timeout_seconds,
        artifact_paths=artifact_paths or {},
        manual=manual,
        required=required,
    )


def _contract(
    runtime: Causality,
    *requirements: VerificationRequirement,
    permissions: PermissionContract | None = None,
    non_goals: tuple[str, ...] = (),
    risk: Risk | str = Risk.LOW,
) -> GoalContract:
    return runtime.create_contract(
        GoalContract(
            title="verify",
            summary="structured evidence",
            risk=risk,
            permissions=permissions or PermissionContract(),
            verification_requirements=tuple(requirements),
            non_goals=non_goals,
        )
    )


def _cite(runtime: Causality, contract: GoalContract, event_hash: str) -> None:
    runtime.record_verifier(
        contract,
        VerifierDecision("correctness", "pass", "command passed", evidence_refs=(event_hash,)),
    )
    runtime.record_verifier(
        contract,
        VerifierDecision("evidence", "pass", "ledger checked", evidence_refs=(event_hash,)),
    )


class VerificationRequirementTests(unittest.TestCase):
    def test_roundtrip_preserves_executable_contract(self) -> None:
        expected = hashlib.sha256(b"ok").hexdigest()
        requirement = _requirement(
            "acceptance",
            argv=_python("print('done')"),
            expected_exit_codes=(0, 2),
            timeout_seconds=12.5,
            artifact_paths={"out/result.txt": expected, "out/report.json": None},
        )
        contract = GoalContract(
            title="roundtrip",
            summary="all fields",
            verification_requirements=(requirement,),
        )

        restored = GoalContract.from_mapping(contract.to_dict())

        self.assertEqual(restored.verification_requirements, (requirement,))
        self.assertEqual(
            restored.to_dict()["verification_requirements"][0]["artifact_paths"],
            {"out/result.txt": expected, "out/report.json": None},
        )

    def test_invalid_requirement_contracts_are_rejected(self) -> None:
        invalid = (
            {"id": "", "argv": ("x",)},
            {"id": "x", "argv": ()},
            {"id": "x", "argv": ("x",), "expected_exit_codes": ()},
            {"id": "x", "argv": ("x",), "timeout_seconds": 0},
            {"id": "x", "argv": ("x",), "manual": True},
            {"id": "x", "argv": (), "manual": True, "artifact_paths": {"a": "bad"}},
            {"id": "x", "argv": (), "manual": True, "artifact_paths": {"a": None}},
            {"id": "x", "argv": "python"},
            {"id": "x", "argv": ("x",), "artifact_paths": "result.txt"},
            {"id": "x", "argv": ("x",), "timeout_seconds": float("nan")},
            {"id": "x", "argv": ("x",), "timeout_seconds": float("inf")},
            {"id": "x", "argv": ("x",), "required": "yes"},
        )
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                VerificationRequirement(**kwargs)

        duplicate = _requirement("same")
        with self.assertRaises(ValueError):
            GoalContract(
                title="duplicate",
                summary="ids",
                verification_requirements=(duplicate, duplicate),
            )
        with self.assertRaises(ValueError):
            GoalContract(
                title="optional-only",
                summary="no completion evidence",
                verification_requirements=(_requirement(required=False),),
            )

    def test_legacy_strings_warn_once_and_receive_unique_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                bound = ContractHarness(runtime).bind(
                    objective="legacy",
                    verification=("python -m unittest", "python -m unittest"),
                    stop_condition={"max_iterations": 2},
                )

            deprecations = [item for item in caught if item.category is DeprecationWarning]
            self.assertEqual(len(deprecations), 1)
            requirements = bound.contract.verification_requirements
            self.assertEqual([item.id for item in requirements], ["verify-001", "verify-002"])
            self.assertEqual(requirements[0].argv, ("python", "-m", "unittest"))

    def test_structured_requirements_do_not_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                bound = ContractHarness(runtime).bind(
                    objective="structured",
                    verification=(_requirement(),),
                    stop_condition={"max_iterations": 2},
                )
            self.assertEqual(caught, [])
            self.assertEqual(bound.task.verification_requirements, (_requirement(),))

    def test_legacy_parser_preserves_quoted_argv(self) -> None:
        argv = (sys.executable, "-c", 'print("hello world")')
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                bound = ContractHarness(runtime).bind(
                    objective="quoted legacy",
                    verification=(command,),
                    stop_condition={"max_iterations": 1},
                )

            self.assertEqual(bound.contract.verification_requirements[0].argv, argv)


class VerificationExecutionTests(unittest.TestCase):
    def test_record_evidence_payload_cannot_override_reserved_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(GoalContract("evidence", "reserved field"))

            event = runtime.record_evidence(
                contract,
                EvidenceKind.TEST_OUTPUT,
                {
                    "kind": EvidenceKind.VERIFICATION_RESULT.value,
                    "evidence_workspace_fingerprint_sha256": "forged",
                },
            )

            self.assertEqual(event.payload["kind"], EvidenceKind.TEST_OUTPUT.value)
            self.assertEqual(
                len(event.payload["evidence_workspace_fingerprint_sha256"]),
                64,
            )

    def test_passing_command_records_exact_result_and_event_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement(
                argv=_python(
                    "import sys; print('hello'); print('warning', file=sys.stderr)"
                )
            )
            contract = _contract(runtime, requirement)

            result = runtime.verify_requirement(contract, "unit", root=root)

            self.assertEqual(result.status, "pass")
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(result.event_hash)
            evidence = runtime.ledger.find(AuditEventType.EVIDENCE)
            self.assertEqual(result.event_hash, evidence[-1].entry_hash)
            self.assertNotEqual(result.event_hash, evidence[-1].event_id)
            self.assertEqual(evidence[-1].payload["requirement_id"], "unit")
            self.assertEqual(evidence[-1].payload["argv"], list(requirement.argv))
            self.assertGreater(evidence[-1].payload["stdout_bytes"], 0)
            self.assertEqual(evidence[-1].payload["stdout"].strip(), "hello")
            self.assertEqual(evidence[-1].payload["stderr"].strip(), "warning")
            self.assertEqual(result.stdout, evidence[-1].payload["stdout"])
            self.assertEqual(result.stderr, evidence[-1].payload["stderr"])
            self.assertEqual(
                result.stdout_sha256,
                hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
            )

    def test_expected_nonzero_exit_can_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement(
                argv=_python("raise SystemExit(2)"), expected_exit_codes=(0, 2)
            )
            contract = _contract(runtime, requirement)

            result = runtime.verify_requirement(contract, "unit", root=root)

            self.assertEqual((result.status, result.exit_code), ("pass", 2))

    def test_nonzero_exit_is_evidence_and_cannot_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement(argv=_python("raise SystemExit(7)")))

            result = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, result.event_hash)

            self.assertEqual((result.status, result.exit_code), ("fail", 7))
            completion = runtime.complete(contract)
            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("unit", " ".join(completion.reasons))

    def test_nonexistent_command_records_error_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(
                runtime,
                _requirement(argv=("causality-command-that-does-not-exist-9345",)),
            )

            result = runtime.verify_requirement(contract, "unit", root=root)

            self.assertEqual(result.status, "error")
            self.assertEqual(contract.state_value, StateTransition.BLOCKED.value)
            evidence = runtime.ledger.find(AuditEventType.EVIDENCE)
            self.assertEqual(evidence[-1].payload["status"], "error")
            self.assertTrue(runtime.ledger.verify_chain())

    def test_timeout_records_evidence_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement(
                argv=_python("import time; time.sleep(1)"), timeout_seconds=0.01
            )
            contract = _contract(runtime, requirement)

            result = runtime.verify_requirement(contract, "unit", root=root)

            self.assertEqual(result.status, "timeout")
            self.assertEqual(contract.state_value, StateTransition.BLOCKED.value)
            self.assertEqual(
                runtime.ledger.find(AuditEventType.EVIDENCE)[-1].payload["status"],
                "timeout",
            )

    def test_gate_block_records_result_without_running_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sentinel = root / "should-not-exist"
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement(
                argv=_python(
                    "from pathlib import Path; Path('should-not-exist').write_text('bad')"
                )
            )
            contract = _contract(
                runtime,
                requirement,
                permissions=PermissionContract(allowed_tools=("git",)),
            )

            result = runtime.verify_requirement(contract, "unit", root=root)

            self.assertEqual(result.status, "blocked")
            self.assertFalse(sentinel.exists())
            self.assertEqual(runtime.ledger.find(AuditEventType.TOOL_CALL), [])
            self.assertEqual(contract.state_value, StateTransition.BLOCKED.value)

    def test_verification_root_is_bound_to_durable_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            other = Path(temp_dir) / "other"
            root.mkdir()
            other.mkdir()
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())

            with self.assertRaises(ValueError):
                runtime.verify_requirement(contract, "unit", root=other)

            self.assertEqual(runtime.ledger.find(AuditEventType.EVIDENCE), [])

    def test_live_permission_widening_cannot_change_frozen_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(
                runtime,
                _requirement(),
                permissions=PermissionContract(allowed_tools=("git",)),
            )
            contract.permissions = PermissionContract()

            with self.assertRaises(ValueError):
                runtime.verify_requirement(contract, "unit", root=root)

    def test_non_goal_matches_declared_argv_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement(
                argv=_python("from pathlib import Path; Path('forbidden').write_text('x')")
            )
            contract = _contract(runtime, requirement, non_goals=("forbidden",))

            result = runtime.verify_requirement(contract, "unit", root=root)

            self.assertEqual(result.status, "blocked")
            self.assertFalse((root / "forbidden").exists())

    def test_argv_is_not_shell_interpreted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sentinel = root / "injected"
            runtime = Causality(root / "ledger.jsonl")
            injection = "&& echo injected > injected"
            requirement = _requirement(
                argv=_python("import sys; print(sys.argv[1])", injection)
            )
            contract = _contract(runtime, requirement)

            result = runtime.verify_requirement(contract, "unit", root=root)

            self.assertEqual(result.status, "pass")
            self.assertFalse(sentinel.exists())

    def test_artifact_existence_and_expected_hash_are_enforced(self) -> None:
        expected = hashlib.sha256(b"correct").hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement(
                argv=_python("from pathlib import Path; Path('result.txt').write_text('wrong')"),
                artifact_paths={"result.txt": expected, "missing.txt": None},
            )
            contract = _contract(runtime, requirement)

            result = runtime.verify_requirement(contract, "unit", root=root)

            self.assertEqual(result.status, "fail")
            self.assertEqual(
                result.artifact_hashes["result.txt"],
                hashlib.sha256(b"wrong").hexdigest(),
            )
            self.assertIsNone(result.artifact_hashes["missing.txt"])
            self.assertIn("artifact", result.reason)

    def test_artifact_path_cannot_escape_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            runtime = Causality(workspace / "ledger.jsonl")
            requirement = _requirement(
                argv=_python("from pathlib import Path; Path('../outside.txt').write_text('x')"),
                artifact_paths={"../outside.txt": None},
            )
            with self.assertRaises(ValueError):
                _contract(runtime, requirement)
            self.assertFalse((Path(temp_dir) / "outside.txt").exists())

    def test_dependency_tree_mutation_is_not_treated_as_volatile_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for directory in (".venv", "node_modules"):
                with self.subTest(directory=directory):
                    workspace = root / directory
                    workspace.mkdir(exist_ok=True)
                    target = workspace / "state.txt"
                    target.write_text("before", encoding="utf-8")
                    runtime = Causality(root / f"{directory.lstrip('.')}.jsonl")
                    requirement = _requirement(
                        argv=_python(
                            "from pathlib import Path; "
                            f"Path({str(target.relative_to(root))!r}).write_text('after')"
                        )
                    )
                    contract = _contract(runtime, requirement)

                    result = runtime.verify_requirement(contract, "unit", root=root)

                    self.assertEqual(result.status, "fail")
                    self.assertIn(directory, result.reason)

    def test_workspace_file_sharing_ledger_prefix_is_still_fingerprinted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement(
                argv=_python(
                    "from pathlib import Path; Path('ledger.jsonl-source.py').write_text('x')"
                )
            )
            contract = _contract(runtime, requirement)

            result = runtime.verify_requirement(contract, "unit", root=root)

            self.assertEqual(result.status, "fail")
            self.assertIn("ledger.jsonl-source.py", result.reason)

    def test_git_hook_and_info_mutations_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for relative in (Path(".git/hooks/pre-commit"), Path(".git/info/exclude")):
                with self.subTest(path=relative.as_posix()):
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("before", encoding="utf-8")
                    ledger = root / f"ledger-{relative.name}.jsonl"
                    runtime = Causality(ledger, project_root=root)
                    requirement = _requirement(
                        argv=_python(
                            "from pathlib import Path; "
                            f"Path({relative.as_posix()!r}).write_text('after')"
                        )
                    )
                    contract = _contract(runtime, requirement)

                    result = runtime.verify_requirement(contract, "unit", root=root)

                    self.assertEqual(result.status, "fail")
                    self.assertIn(relative.as_posix(), result.reason)

    def test_linked_worktree_gitdir_mutation_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "worktree"
            gitdir = base / "metadata" / "worktrees" / "feature"
            root.mkdir()
            gitdir.mkdir(parents=True)
            (root / ".git").write_text(
                f"gitdir: {os.path.relpath(gitdir, root)}\n",
                encoding="utf-8",
            )
            (gitdir / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
            (gitdir / "index").write_text("before", encoding="utf-8")
            runtime = Causality(root / "ledger.jsonl", project_root=root)
            relative_index = os.path.relpath(gitdir / "index", root)
            requirement = _requirement(
                argv=_python(
                    "from pathlib import Path; "
                    f"Path({relative_index!r}).write_text('after')"
                )
            )
            contract = _contract(runtime, requirement)

            result = runtime.verify_requirement(contract, "unit", root=root)

            self.assertEqual(result.status, "fail")
            self.assertIn(".gitdir/index", result.reason)

    def test_linked_worktree_common_git_metadata_mutation_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "worktree"
            common = base / "metadata"
            gitdir = common / "worktrees" / "feature"
            root.mkdir()
            gitdir.mkdir(parents=True)
            hook = common / "hooks" / "pre-commit"
            hook.parent.mkdir()
            hook.write_text("before", encoding="utf-8")
            (root / ".git").write_text(
                f"gitdir: {os.path.relpath(gitdir, root)}\n",
                encoding="utf-8",
            )
            (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
            (gitdir / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
            runtime = Causality(root / "ledger.jsonl", project_root=root)
            requirement = _requirement(
                argv=_python(
                    "from pathlib import Path; "
                    f"Path({os.path.relpath(hook, root)!r}).write_text('after')"
                )
            )
            contract = _contract(runtime, requirement)

            result = runtime.verify_requirement(contract, "unit", root=root)

            self.assertEqual(result.status, "fail")
            self.assertIn(".git-common/hooks/pre-commit", result.reason)

    def test_external_symlink_target_mutation_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as external_dir:
            root = Path(temp_dir)
            external = Path(external_dir) / "input.txt"
            external.write_text("before", encoding="utf-8")
            link = root / "linked-input.txt"
            try:
                link.symlink_to(external)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement(
                argv=_python(
                    "from pathlib import Path; "
                    f"Path({str(external)!r}).write_text('after')"
                )
            )
            contract = _contract(runtime, requirement)

            result = runtime.verify_requirement(contract, "unit", root=root)

            self.assertEqual(result.status, "fail")
            self.assertIn("linked-input.txt", result.reason)

    def test_external_directory_symlink_target_mutation_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as external_dir:
            root = Path(temp_dir)
            external = Path(external_dir)
            target = external / "input.txt"
            target.write_text("before", encoding="utf-8")
            link = root / "linked-inputs"
            try:
                link.symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement(
                argv=_python(
                    "from pathlib import Path; "
                    f"Path({str(target)!r}).write_text('after')"
                )
            )
            contract = _contract(runtime, requirement)

            result = runtime.verify_requirement(contract, "unit", root=root)

            self.assertEqual(result.status, "fail")
            self.assertIn("linked-inputs", result.reason)


class VerificationCompletionTests(unittest.TestCase):
    def test_one_result_cannot_satisfy_two_requirement_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement("unit"), _requirement("integration"))
            result = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, result.event_hash)

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("integration", " ".join(completion.reasons))

    def test_valid_task_scoped_result_and_two_cited_verifiers_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, result.event_hash)

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.PASS)

    def test_min_passes_cannot_lower_two_verifier_floor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            runtime.verify_requirement(contract, "unit", root=root)

            completion = runtime.complete(contract, min_passes=0)

            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("at least 2", " ".join(completion.reasons))

    def test_duplicate_verifier_names_are_rejected_in_current_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)
            duplicate = VerifierDecision(
                "same",
                "pass",
                "cited",
                evidence_refs=(result.event_hash,),
            )

            runtime.record_verifier(contract, duplicate)
            runtime.record_verifier(contract, duplicate)
            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("duplicate verifier", " ".join(completion.reasons))

            fresh = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, fresh.event_hash)
            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_each_verifier_must_cite_every_required_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement("unit"), _requirement("integration"))
            unit = runtime.verify_requirement(contract, "unit", root=root)
            runtime.verify_requirement(contract, "integration", root=root)
            runtime.record_verifier(
                contract,
                VerifierDecision(
                    "correctness",
                    "pass",
                    "reviewed only unit",
                    evidence_refs=(unit.event_hash,),
                ),
            )
            runtime.record_verifier(
                contract,
                VerifierDecision(
                    "evidence",
                    "pass",
                    "reviewed only unit",
                    evidence_refs=(unit.event_hash,),
                ),
            )

            incomplete = runtime.complete(contract)
            self.assertEqual(incomplete.decision, GateDecision.REPAIR)
            self.assertIn("citation", " ".join(incomplete.reasons))

            unit = runtime.verify_requirement(contract, "unit", root=root)
            integration = runtime.verify_requirement(contract, "integration", root=root)
            all_refs = (unit.event_hash, integration.event_hash)
            runtime.record_verifier(
                contract,
                VerifierDecision(
                    "correctness", "pass", "reviewed both", evidence_refs=all_refs
                ),
            )
            runtime.record_verifier(
                contract,
                VerifierDecision(
                    "evidence", "pass", "reviewed both", evidence_refs=all_refs
                ),
            )

            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_coexisting_generic_evidence_must_be_fresh_and_cited(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = runtime.create_contract(
                GoalContract(
                    "mixed evidence",
                    "structured and generic requirements",
                    evidence_required=[
                        EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "external report")
                    ],
                    verification_requirements=(_requirement(),),
                )
            )
            runtime.record_evidence(contract, EvidenceKind.TEST_OUTPUT, {"summary": "old"})
            runtime.verify_requirement(contract, "unit", root=root)
            tools = ToolAdapter(
                runtime.ledger,
                ExecutionAdapter(runtime, contract),
                root=root,
                runner=lambda _argv: CommandResult(0, "", ""),
            )
            tools.run(["noop"])
            result = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, result.event_hash)

            stale = runtime.complete(contract)
            self.assertEqual(stale.decision, GateDecision.REPAIR)
            self.assertIn("generic evidence", " ".join(stale.reasons))

            generic = runtime.record_evidence(
                contract,
                EvidenceKind.TEST_OUTPUT,
                {"summary": "fresh"},
            )
            all_refs = (result.event_hash, generic.entry_hash)
            runtime.record_verifier(
                contract,
                VerifierDecision("correctness", "pass", "all evidence", evidence_refs=all_refs),
            )
            runtime.record_verifier(
                contract,
                VerifierDecision("evidence", "pass", "all evidence", evidence_refs=all_refs),
            )

            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_generic_only_evidence_must_be_fresh_and_cited(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = runtime.create_contract(
                GoalContract(
                    "generic evidence",
                    "fresh cited output",
                    evidence_required=[
                        EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "external report")
                    ],
                )
            )
            runtime.record_evidence(
                contract,
                EvidenceKind.TEST_OUTPUT,
                {"summary": "old"},
            )
            (root / "changed.txt").write_text("changed", encoding="utf-8")
            runtime.record_verifier(contract, VerifierDecision("one", "pass", "looks good"))
            runtime.record_verifier(contract, VerifierDecision("two", "pass", "also good"))

            stale = runtime.complete(contract)
            self.assertEqual(stale.decision, GateDecision.REPAIR)
            self.assertIn("stale generic evidence workspace", " ".join(stale.reasons))
            self.assertIn("citation", " ".join(stale.reasons))

            fresh = runtime.record_evidence(
                contract,
                EvidenceKind.TEST_OUTPUT,
                {"summary": "fresh"},
            )
            _cite(runtime, contract, fresh.entry_hash)

            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_optional_generic_evidence_does_not_become_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Causality(Path(temp_dir) / "ledger.jsonl")
            contract = runtime.create_contract(
                GoalContract(
                    "optional evidence",
                    "legacy verifier completion",
                    evidence_required=[
                        EvidenceRequirement(
                            EvidenceKind.TEST_OUTPUT,
                            "optional report",
                            required=False,
                        )
                    ],
                )
            )
            runtime.record_verifier(contract, VerifierDecision("one", "pass", "reviewed"))
            runtime.record_verifier(contract, VerifierDecision("two", "pass", "reviewed"))

            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_post_write_evidence_is_fresh_for_its_own_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = runtime.create_contract(
                GoalContract(
                    "artifact evidence",
                    "write then verify",
                    evidence_required=[
                        EvidenceRequirement(EvidenceKind.ARTIFACT_HASH, "written output")
                    ],
                    verification_requirements=(_requirement(),),
                )
            )
            tools = ToolAdapter(
                runtime.ledger,
                ExecutionAdapter(runtime, contract),
                root=root,
            )
            tools.write_text("output.txt", "done")
            artifact_hash = tools.last_event_hash
            self.assertIsNotNone(artifact_hash)
            result = runtime.verify_requirement(contract, "unit", root=root)
            refs = (artifact_hash, result.event_hash)
            for name in ("correctness", "evidence"):
                runtime.record_verifier(
                    contract,
                    VerifierDecision(name, "pass", "reviewed", evidence_refs=refs),
                )

            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_manual_evidence_is_fresh_for_its_own_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement("visual", manual=True))
            evidence = runtime.record_evidence(
                contract,
                EvidenceKind.A11Y_REPORT,
                {"summary": "captured", "mutates_task": True},
            )
            runtime.record_manual_verification(
                contract,
                "visual",
                evidence_hash=evidence.entry_hash,
                approved=True,
                approver="alice",
                rationale="reviewed",
            )
            _cite(runtime, contract, evidence.entry_hash)

            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_legacy_completion_uses_durable_binding_and_two_verifier_floor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = runtime.create_contract(
                GoalContract(
                    "legacy high risk",
                    "must stay bound",
                    risk=Risk.HIGH,
                    evidence_required=[
                        EvidenceRequirement(EvidenceKind.TEST_OUTPUT, "tests")
                    ],
                )
            )
            contract.risk = Risk.LOW
            contract.evidence_required.clear()
            with self.assertRaises(ValueError):
                runtime.record_verifier(
                    contract,
                    VerifierDecision("one", "pass", "claimed"),
                )

            mismatch = runtime.complete(contract)
            self.assertEqual(mismatch.decision, GateDecision.REPAIR)
            self.assertIn("snapshot", " ".join(mismatch.reasons))

            fresh = runtime.create_contract(GoalContract("legacy", "quorum floor"))
            lowered = runtime.gate.complete(fresh, min_passes=0)
            self.assertEqual(lowered.decision, GateDecision.REPAIR)
            self.assertIn("at least 2", " ".join(lowered.reasons))

    def test_completion_detects_tox_and_bytecode_state_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "victim.py").write_text("VALUE = 'good'\n", encoding="utf-8")
            tox_state = root / ".tox" / "py" / "installed.txt"
            tox_state.parent.mkdir(parents=True)
            tox_state.write_text("before", encoding="utf-8")
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement(
                argv=_python("import victim; raise SystemExit(victim.VALUE != 'good')")
            )
            contract = _contract(runtime, requirement)
            result = runtime.verify_requirement(contract, "unit", root=root)
            self.assertEqual(result.status, "pass")
            self.assertFalse((root / "__pycache__").exists())

            tox_state.write_text("after", encoding="utf-8")
            cache = root / "__pycache__" / "victim.pyc"
            cache.parent.mkdir()
            py_compile.compile(str(root / "victim.py"), cfile=str(cache), doraise=True)
            _cite(runtime, contract, result.event_hash)

            completion = runtime.complete(contract)
            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("workspace", " ".join(completion.reasons))

    def test_completion_detects_git_object_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            git_object = root / ".git" / "objects" / "aa" / "object"
            git_object.parent.mkdir(parents=True)
            git_object.write_bytes(b"object contents")
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)

            git_object.unlink()
            _cite(runtime, contract, result.event_hash)

            completion = runtime.complete(contract)
            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("workspace", " ".join(completion.reasons))

    def test_verifier_identity_is_trimmed_and_case_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)
            for name in ("same", " SAME "):
                runtime.record_verifier(
                    contract,
                    VerifierDecision(
                        name,
                        "pass",
                        "cited",
                        evidence_refs=(result.event_hash,),
                    ),
                )

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("duplicate verifier", " ".join(completion.reasons))

    def test_unrecorded_supplied_verdicts_cannot_complete_structured_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)
            supplied = [
                VerifierDecision("v1", "pass", "x", evidence_refs=(result.event_hash,)),
                VerifierDecision("v2", "pass", "y", evidence_refs=(result.event_hash,)),
            ]

            completion = runtime.gate.complete(contract, supplied)

            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("ledger-recorded", " ".join(completion.reasons))

    def test_structured_contract_rejects_rationale_only_and_foreign_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)
            runtime.record_verifier(contract, VerifierDecision("v1", "pass", "looks good"))
            runtime.record_verifier(contract, VerifierDecision("v2", "pass", "also good"))
            self.assertEqual(runtime.complete(contract).decision, GateDecision.REPAIR)

            other = _contract(runtime, _requirement("other"))
            foreign = runtime.record_evidence(other, EvidenceKind.TEST_OUTPUT, {"output": "ok"})
            runtime.record_verifier(
                contract,
                VerifierDecision("v1", "pass", "foreign", evidence_refs=(foreign.entry_hash,)),
            )
            runtime.record_verifier(
                contract,
                VerifierDecision("v2", "pass", "forged", evidence_refs=("a" * 64,)),
            )

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("citation", " ".join(completion.reasons))
            self.assertTrue(result.event_hash)

    def test_blank_verifier_identity_never_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)
            for name in ("", "   "):
                runtime.record_verifier(
                    contract,
                    VerifierDecision(
                        name,
                        "pass",
                        "anonymous",
                        evidence_refs=(result.event_hash,),
                    ),
                )

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("identity", " ".join(completion.reasons))

    def test_event_id_and_non_evidence_hash_are_not_valid_citations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)
            tool_event = runtime.ledger.append(
                AuditEventType.TOOL_CALL,
                {"tool": "read", "mutates_task": False},
                contract_id=contract.goal_id,
            )
            runtime.record_verifier(
                contract,
                VerifierDecision("v1", "pass", "event id", evidence_refs=(
                    runtime.ledger.find(AuditEventType.EVIDENCE)[-1].event_id,
                )),
            )
            runtime.record_verifier(
                contract,
                VerifierDecision("v2", "pass", "tool hash", evidence_refs=(tool_event.entry_hash,)),
            )

            self.assertEqual(runtime.complete(contract).decision, GateDecision.REPAIR)
            self.assertTrue(result.event_hash)

    def test_generic_evidence_append_cannot_forge_executable_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement()
            contract = _contract(runtime, requirement)
            forged = runtime.record_evidence(
                contract,
                EvidenceKind.VERIFICATION_RESULT,
                {
                    "requirement_id": "unit",
                    "manual": False,
                    "status": "pass",
                    "argv": list(requirement.argv),
                    "expected_exit_codes": [0],
                    "exit_code": 0,
                    "stdout_bytes": 0,
                    "stderr_bytes": 0,
                    "artifact_records": [],
                    "completed_at": "2026-07-11T00:00:00+00:00",
                    "mutates_task": False,
                },
            )
            _cite(runtime, contract, forged.entry_hash)

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("tool provenance", " ".join(completion.reasons))

    def test_latest_result_wins_and_mutation_makes_it_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            flag = root / "flag"
            flag.write_text("yes", encoding="utf-8")
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement(
                argv=_python(
                    "from pathlib import Path; raise SystemExit(0 if Path('flag').exists() else 9)"
                )
            )
            contract = _contract(runtime, requirement)

            first = runtime.verify_requirement(contract, "unit", root=root)
            flag.unlink()
            failed = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, failed.event_hash)
            self.assertEqual(first.status, "pass")
            self.assertEqual(failed.status, "fail")
            self.assertEqual(runtime.complete(contract).decision, GateDecision.REPAIR)

            flag.write_text("yes", encoding="utf-8")
            fresh = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, fresh.event_hash)
            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

            runtime.ledger.append(
                AuditEventType.TOOL_CALL,
                {"tool": "file.write", "mutates_task": True},
                contract_id=contract.goal_id,
            )
            self.assertEqual(runtime.complete(contract).decision, GateDecision.REPAIR)
            self.assertIn(
                "stale",
                " ".join(runtime.complete(contract).reasons).lower(),
            )

    def test_later_verification_cannot_silently_mutate_earlier_check_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "state").write_text("good", encoding="utf-8")
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(
                runtime,
                _requirement(
                    "check-state",
                    argv=_python(
                        "from pathlib import Path; raise SystemExit(0 if "
                        "Path('state').read_text() == 'good' else 1)"
                    ),
                ),
                _requirement(
                    "mutate-state",
                    argv=_python("from pathlib import Path; Path('state').write_text('bad')"),
                ),
            )

            first = runtime.verify_requirement(contract, "check-state", root=root)
            second = runtime.verify_requirement(contract, "mutate-state", root=root)

            self.assertEqual(first.status, "pass")
            self.assertEqual(second.status, "fail")
            self.assertIn("changed undeclared workspace", second.reason)
            self.assertEqual(contract.state_value, StateTransition.BLOCKED.value)
            self.assertNotEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_declared_artifact_change_invalidates_earlier_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shared = root / "shared.txt"
            shared.write_text("good", encoding="utf-8")
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(
                runtime,
                _requirement(
                    "check",
                    argv=_python(
                        "from pathlib import Path; raise SystemExit(0 if "
                        "Path('shared.txt').read_text() == 'good' else 1)"
                    ),
                ),
                _requirement(
                    "artifact",
                    argv=_python(
                        "from pathlib import Path; Path('shared.txt').write_text('bad')"
                    ),
                    artifact_paths={"shared.txt": hashlib.sha256(b"bad").hexdigest()},
                ),
            )

            first = runtime.verify_requirement(contract, "check", root=root)
            second = runtime.verify_requirement(contract, "artifact", root=root)
            _cite(runtime, contract, second.event_hash)

            completion = runtime.complete(contract)

            self.assertEqual(first.status, "pass")
            self.assertEqual(second.status, "pass")
            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("check: verification result is stale", completion.reasons)

    def test_completion_reports_each_unmet_requirement_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(
                runtime,
                _requirement("missing"),
                _requirement("failed", argv=_python("raise SystemExit(4)")),
                _requirement("stale"),
            )
            runtime.verify_requirement(contract, "failed", root=root)
            runtime.verify_requirement(contract, "stale", root=root)
            runtime.ledger.append(
                AuditEventType.TOOL_CALL,
                {"tool": "file.write", "mutates_task": True},
                contract_id=contract.goal_id,
            )

            completion = runtime.complete(contract)

            joined = " ".join(completion.reasons)
            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("missing", joined)
            self.assertIn("failed", joined)
            self.assertIn("stale", joined)

    def test_read_only_events_do_not_make_result_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)
            runtime.ledger.append(
                AuditEventType.TOOL_CALL,
                {"tool": "file.read", "mutates_task": False},
                contract_id=contract.goal_id,
            )
            _cite(runtime, contract, result.event_hash)

            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_public_tool_run_always_stales_prior_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)
            tools = ToolAdapter(runtime.ledger, ExecutionAdapter(runtime, contract), root=root)

            tools.run(_python("from pathlib import Path; Path('changed.txt').write_text('x')"))
            _cite(runtime, contract, result.event_hash)

            completion = runtime.complete(contract)
            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("stale", " ".join(completion.reasons))

    def test_completion_detects_direct_unlogged_workspace_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)

            (root / "unlogged.txt").write_text("changed", encoding="utf-8")
            _cite(runtime, contract, result.event_hash)

            completion = runtime.complete(contract)
            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("workspace", " ".join(completion.reasons))

    def test_completion_detects_execution_adapter_workspace_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)
            adapter = ExecutionAdapter(runtime, contract)

            adapter.execute(
                tool="file.write",
                action_kind="write",
                description="write untracked file",
                run=lambda: (root / "adapter-write.txt").write_text("changed", encoding="utf-8"),
            )
            _cite(runtime, contract, result.event_hash)

            completion = runtime.complete(contract)
            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("workspace", " ".join(completion.reasons))

    def test_artifact_changed_after_verification_is_rejected(self) -> None:
        expected = hashlib.sha256(b"correct").hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement(
                argv=_python("from pathlib import Path; Path('result.txt').write_text('correct')"),
                artifact_paths={"result.txt": expected},
            )
            contract = _contract(runtime, requirement)
            result = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, result.event_hash)
            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

            (root / "result.txt").write_text("tampered", encoding="utf-8")

            completion = runtime.complete(contract)
            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("artifact", " ".join(completion.reasons))

    def test_artifact_mode_changed_after_verification_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement(
                argv=_python("from pathlib import Path; Path('result.txt').write_text('same')"),
                artifact_paths={"result.txt": hashlib.sha256(b"same").hexdigest()},
            )
            contract = _contract(runtime, requirement)
            result = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, result.event_hash)
            target = root / "result.txt"
            original = stat.S_IMODE(target.lstat().st_mode)
            target.chmod(original ^ stat.S_IWUSR)
            if stat.S_IMODE(target.lstat().st_mode) == original:
                self.skipTest("filesystem does not expose chmod mode changes")

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("mode", " ".join(completion.reasons))

    def test_artifact_cannot_be_replaced_by_external_same_content_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as external_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            requirement = _requirement(
                argv=_python("from pathlib import Path; Path('result.txt').write_text('same')"),
                artifact_paths={"result.txt": hashlib.sha256(b"same").hexdigest()},
            )
            contract = _contract(runtime, requirement)
            result = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, result.event_hash)
            external = Path(external_dir) / "result.txt"
            external.write_text("same", encoding="utf-8")
            target = root / "result.txt"
            target.unlink()
            try:
                target.symlink_to(external)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("artifact", " ".join(completion.reasons))

    def test_manual_verdict_requires_same_contract_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            manual = _requirement("visual", manual=True)
            contract = _contract(runtime, manual)
            evidence = runtime.record_evidence(contract, EvidenceKind.A11Y_REPORT, {"summary": "ok"})
            runtime.record_manual_verification(
                contract,
                "visual",
                evidence_hash=evidence.entry_hash,
                approved=True,
                approver="alice",
                rationale="screen reviewed",
            )
            _cite(runtime, contract, evidence.entry_hash)

            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

            executable_contract = _contract(runtime, _requirement("unit"))
            with self.assertRaises(ValueError):
                runtime.record_manual_verification(
                    executable_contract,
                    "unit",
                    evidence_hash=evidence.entry_hash,
                    approved=True,
                    approver="alice",
                    rationale="not executable",
                )

    def test_manual_only_completion_detects_post_approval_workspace_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reviewed = root / "reviewed.txt"
            reviewed.write_text("approved", encoding="utf-8")
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement("visual", manual=True))
            evidence = runtime.record_evidence(
                contract,
                EvidenceKind.ARTIFACT_HASH,
                {"summary": "reviewed file"},
                artifact_paths=(reviewed,),
            )
            runtime.record_manual_verification(
                contract,
                "visual",
                evidence_hash=evidence.entry_hash,
                approved=True,
                approver="alice",
                rationale="reviewed",
            )
            reviewed.write_text("changed after approval", encoding="utf-8")
            _cite(runtime, contract, evidence.entry_hash)

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("workspace changed", " ".join(completion.reasons))

    def test_manual_verdict_rejects_workspace_change_after_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reviewed = root / "reviewed.txt"
            reviewed.write_text("evidence state", encoding="utf-8")
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement("visual", manual=True))
            evidence = runtime.record_evidence(
                contract,
                EvidenceKind.ARTIFACT_HASH,
                {"summary": "reviewed file"},
                artifact_paths=(reviewed,),
            )
            reviewed.write_text("changed before approval", encoding="utf-8")
            runtime.record_manual_verification(
                contract,
                "visual",
                evidence_hash=evidence.entry_hash,
                approved=True,
                approver="alice",
                rationale="reviewed",
            )
            _cite(runtime, contract, evidence.entry_hash)

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("evidence workspace", " ".join(completion.reasons))

    def test_manual_verdict_requires_boolean_and_named_human(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement("visual", manual=True))
            evidence = runtime.record_evidence(
                contract,
                EvidenceKind.A11Y_REPORT,
                {"summary": "screen captured"},
            )

            with self.assertRaises(ValueError):
                runtime.record_manual_verification(
                    contract,
                    "visual",
                    evidence_hash=evidence.entry_hash,
                    approved="false",  # type: ignore[arg-type]
                    approver="alice",
                    rationale="reviewed",
                )
            with self.assertRaises(ValueError):
                runtime.record_manual_verification(
                    contract,
                    "visual",
                    evidence_hash=evidence.entry_hash,
                    approved=True,
                    approver="   ",
                    rationale="reviewed",
                )

            runtime.ledger.append(
                AuditEventType.HUMAN_DECISION,
                {
                    "stage": "verification:visual",
                    "manual": True,
                    "approved": True,
                    "approver": "",
                    "rationale": "forged",
                    "evidence_hash": evidence.entry_hash,
                },
                contract_id=contract.goal_id,
            )
            _cite(runtime, contract, evidence.entry_hash)

            completion = runtime.complete(contract)
            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("approver", " ".join(completion.reasons))

    def test_plan_gate_rejects_live_risk_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement(), risk=Risk.HIGH)
            contract.risk = Risk.LOW

            decision = runtime.evaluate_plan(contract)

            self.assertEqual(decision.decision, GateDecision.REPAIR)
            self.assertIn("snapshot", " ".join(decision.reasons))

    def test_plan_gate_rejects_any_live_binding_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            contract.non_goals = ("new exclusion",)

            decision = runtime.evaluate_plan(contract)

            self.assertEqual(decision.decision, GateDecision.REPAIR)
            self.assertIn("snapshot", " ".join(decision.reasons))

    def test_plan_gate_stops_without_appending_to_broken_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / "ledger.jsonl"
            runtime = Causality(ledger_path)
            contract = _contract(runtime, _requirement())
            content = ledger_path.read_text(encoding="utf-8")
            ledger_path.write_text(
                content.replace('"title": "verify"', '"title": "tampered"', 1),
                encoding="utf-8",
            )
            size_before = ledger_path.stat().st_size

            decision = runtime.evaluate_plan(contract)

            self.assertEqual(decision.decision, GateDecision.STOP)
            self.assertIn("hash chain", " ".join(decision.reasons))
            self.assertEqual(ledger_path.stat().st_size, size_before)

    def test_manual_verdict_rejects_foreign_evidence_and_latest_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement("visual", manual=True))
            other = _contract(runtime, _requirement("other"))
            foreign = runtime.record_evidence(other, EvidenceKind.A11Y_REPORT, {"summary": "x"})
            runtime.record_manual_verification(
                contract,
                "visual",
                evidence_hash=foreign.entry_hash,
                approved=True,
                approver="alice",
                rationale="wrong task",
            )
            self.assertEqual(runtime.complete(contract).decision, GateDecision.REPAIR)

            local = runtime.record_evidence(
                contract,
                EvidenceKind.A11Y_REPORT,
                {"summary": "ok"},
            )
            runtime.record_manual_verification(
                contract,
                "visual",
                evidence_hash=local.entry_hash,
                approved=False,
                approver="alice",
                rationale="needs repair",
            )
            _cite(runtime, contract, local.entry_hash)
            self.assertEqual(runtime.complete(contract).decision, GateDecision.REPAIR)

            runtime.record_manual_verification(
                contract,
                "visual",
                evidence_hash=local.entry_hash,
                approved=True,
                approver="alice",
                rationale="rechecked",
            )
            _cite(runtime, contract, local.entry_hash)
            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_requirement_snapshot_cannot_be_removed_after_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            contract.verification_requirements = ()
            with self.assertRaises(ValueError):
                runtime.record_verifier(
                    contract,
                    VerifierDecision("v1", "pass", "bypass"),
                )

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.REPAIR)
            self.assertIn("snapshot", " ".join(completion.reasons))

    def test_risk_downgrade_cannot_remove_final_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement(), risk=Risk.HIGH)
            runtime.approve(contract, "plan", "alice", "approved")
            result = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, result.event_hash)
            contract.risk = Risk.LOW

            completion = runtime.complete(contract)

            self.assertNotEqual(completion.decision, GateDecision.PASS)
            self.assertIn("snapshot", " ".join(completion.reasons))

    def test_high_risk_manual_completion_requires_prior_plan_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(
                runtime,
                _requirement("visual", manual=True),
                risk=Risk.HIGH,
            )
            evidence = runtime.record_evidence(
                contract,
                EvidenceKind.A11Y_REPORT,
                {"summary": "reviewed"},
            )
            runtime.record_manual_verification(
                contract,
                "visual",
                evidence_hash=evidence.entry_hash,
                approved=True,
                approver="alice",
                rationale="reviewed",
            )
            _cite(runtime, contract, evidence.entry_hash)
            runtime.approve(
                contract,
                "final",
                "alice",
                "reviewed",
                evidence_refs=(evidence.entry_hash,),
            )

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.ESCALATE)
            self.assertIn("plan approval", " ".join(completion.reasons))

    def test_late_plan_reapproval_does_not_authorize_earlier_manual_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(
                runtime,
                _requirement("visual", manual=True),
                risk=Risk.HIGH,
            )
            runtime.approve(contract, "plan", "alice", "initial approval")
            runtime.reject(contract, "plan", "alice", "approval withdrawn")
            evidence = runtime.record_evidence(
                contract,
                EvidenceKind.A11Y_REPORT,
                {"summary": "reviewed"},
            )
            runtime.record_manual_verification(
                contract,
                "visual",
                evidence_hash=evidence.entry_hash,
                approved=True,
                approver="alice",
                rationale="reviewed while plan was rejected",
            )
            runtime.approve(contract, "plan", "alice", "too late")
            _cite(runtime, contract, evidence.entry_hash)
            runtime.approve(
                contract,
                "final",
                "alice",
                "reviewed",
                evidence_refs=(evidence.entry_hash,),
            )

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.ESCALATE)
            self.assertIn("active", " ".join(completion.reasons))

    def test_plan_must_remain_approved_at_every_manual_work_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(
                runtime,
                _requirement("visual", manual=True),
                risk=Risk.HIGH,
            )
            runtime.approve(contract, "plan", "alice", "round one approved")
            first = runtime.record_evidence(
                contract,
                EvidenceKind.A11Y_REPORT,
                {"summary": "round one"},
            )
            runtime.record_manual_verification(
                contract,
                "visual",
                evidence_hash=first.entry_hash,
                approved=True,
                approver="alice",
                rationale="round one",
            )
            runtime.reject(contract, "plan", "alice", "approval withdrawn")
            second = runtime.record_evidence(
                contract,
                EvidenceKind.A11Y_REPORT,
                {"summary": "round two"},
            )
            runtime.record_manual_verification(
                contract,
                "visual",
                evidence_hash=second.entry_hash,
                approved=True,
                approver="alice",
                rationale="round two while rejected",
            )
            runtime.approve(contract, "plan", "alice", "too late")
            _cite(runtime, contract, second.entry_hash)
            runtime.approve(
                contract,
                "final",
                "alice",
                "reviewed",
                evidence_refs=(second.entry_hash,),
            )

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.ESCALATE)
            self.assertIn("every task work event", " ".join(completion.reasons))

    def test_high_risk_final_approval_follows_review_and_cites_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement(), risk=Risk.HIGH)
            runtime.approve(contract, "plan", "alice", "plan reviewed")
            result = runtime.verify_requirement(contract, "unit", root=root)
            runtime.approve(
                contract,
                "final",
                "alice",
                "too early",
                evidence_refs=(result.event_hash,),
            )
            _cite(runtime, contract, result.event_hash)
            self.assertEqual(runtime.complete(contract).decision, GateDecision.ESCALATE)

            runtime.approve(
                contract,
                "final",
                "alice",
                "reviewed current verdicts",
                evidence_refs=(result.event_hash,),
            )
            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_high_risk_final_approval_cites_entire_current_evidence_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(
                runtime,
                _requirement("unit"),
                _requirement("integration"),
                risk=Risk.HIGH,
            )
            runtime.approve(contract, "plan", "alice", "approved")
            unit = runtime.verify_requirement(contract, "unit", root=root)
            integration = runtime.verify_requirement(contract, "integration", root=root)
            all_refs = (unit.event_hash, integration.event_hash)
            for name in ("correctness", "evidence"):
                runtime.record_verifier(
                    contract,
                    VerifierDecision(name, "pass", "reviewed all", evidence_refs=all_refs),
                )
            runtime.approve(
                contract,
                "final",
                "alice",
                "partial review",
                evidence_refs=(unit.event_hash,),
            )

            self.assertEqual(runtime.complete(contract).decision, GateDecision.ESCALATE)

            runtime.approve(
                contract,
                "final",
                "alice",
                "reviewed all current evidence",
                evidence_refs=all_refs,
            )
            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)

    def test_duplicate_goal_id_cannot_reuse_another_task_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            first = _contract(runtime, _requirement())
            result = runtime.verify_requirement(first, "unit", root=root)
            _cite(runtime, first, result.event_hash)
            self.assertEqual(runtime.complete(first).decision, GateDecision.PASS)

            second = GoalContract(
                title="different task",
                summary="must not inherit",
                goal_id=first.goal_id,
                verification_requirements=(_requirement(),),
            )
            with self.assertRaises(ValueError):
                runtime.create_contract(second)

    def test_concurrent_duplicate_contract_creation_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / "ledger.jsonl"
            first = Causality(ledger_path)
            second = Causality(ledger_path)
            created_at = "2026-01-01T00:00:00+00:00"

            def contract() -> GoalContract:
                return GoalContract(
                    "same",
                    "same",
                    goal_id="shared",
                    created_at=created_at,
                    verification_requirements=(_requirement(),),
                )

            append_entered = threading.Event()
            release_append = threading.Event()
            second_done = threading.Event()
            original_append = first.ledger.append

            def delayed_append(*args, **kwargs):
                append_entered.set()
                self.assertTrue(release_append.wait(5))
                return original_append(*args, **kwargs)

            first.ledger.append = delayed_append  # type: ignore[method-assign]
            results: list[str] = []

            def create(runtime: Causality, value: GoalContract, done=None) -> None:
                try:
                    runtime.create_contract(value)
                    results.append("ok")
                except ValueError:
                    results.append("duplicate")
                finally:
                    if done is not None:
                        done.set()

            first_thread = threading.Thread(target=create, args=(first, contract()))
            first_thread.start()
            self.assertTrue(append_entered.wait(5))
            second_thread = threading.Thread(
                target=create,
                args=(second, contract(), second_done),
            )
            second_thread.start()
            interleaved = second_done.wait(0.2)
            release_append.set()
            first_thread.join(5)
            second_thread.join(5)

            self.assertFalse(interleaved)
            self.assertEqual(sorted(results), ["ok", "ok"])
            contracts = [
                event
                for event in first.ledger.events_for_contract("shared", all_segments=True)
                if event.event_type == AuditEventType.GOAL_CONTRACT.value
            ]
            self.assertEqual(len(contracts), 1)

    def test_contract_creation_retry_recovers_pending_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = GoalContract(
                "recoverable",
                "anchor failure",
                verification_requirements=(_requirement(),),
            )
            real_write = ledger_module.write_text_durably
            calls = 0

            def fail_final_anchor(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated final anchor failure")
                return real_write(*args, **kwargs)

            with mock.patch.object(
                ledger_module,
                "write_text_durably",
                side_effect=fail_final_anchor,
            ):
                with self.assertRaises(OSError):
                    runtime.create_contract(contract)

            recovered = runtime.create_contract(contract)

            self.assertEqual(recovered.goal_id, contract.goal_id)
            self.assertTrue(runtime.ledger.verify_chain())
            contracts = [
                event
                for event in runtime.ledger.events_for_contract(
                    contract.goal_id,
                    all_segments=True,
                )
                if event.event_type == AuditEventType.GOAL_CONTRACT.value
            ]
            self.assertEqual(len(contracts), 1)
            self.assertEqual(runtime.evaluate_plan(recovered).decision, GateDecision.PASS)

    def test_completion_serializes_with_public_mutating_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, result.event_hash)
            entered = threading.Event()
            release = threading.Event()
            action_done = threading.Event()
            original = runtime.gate._structured_requirement_issues
            tools = ToolAdapter(
                runtime.ledger,
                ExecutionAdapter(runtime, contract),
                root=root,
                runner=lambda _argv: CommandResult(0, "", ""),
            )

            def delayed(requirements, events, *, workspace_root, last_mutation):
                entered.set()
                self.assertTrue(release.wait(5))
                return original(
                    requirements,
                    events,
                    workspace_root=workspace_root,
                    last_mutation=last_mutation,
                )

            runtime.gate._structured_requirement_issues = delayed  # type: ignore[method-assign]
            decisions = []

            def complete() -> None:
                decisions.append(runtime.complete(contract))

            completion_thread = threading.Thread(target=complete)
            completion_thread.start()
            self.assertTrue(entered.wait(5))

            def mutate() -> None:
                tools.run(["noop"])
                action_done.set()

            action_thread = threading.Thread(target=mutate)
            action_thread.start()
            interleaved = action_done.wait(0.2)
            release.set()
            completion_thread.join(5)
            action_thread.join(5)

            self.assertFalse(interleaved)
            self.assertEqual(decisions[0].decision, GateDecision.PASS)
            self.assertTrue(action_done.is_set())
            self.assertEqual(runtime.complete(contract).decision, GateDecision.REPAIR)

    def test_broken_ledger_chain_stops_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / "ledger.jsonl"
            runtime = Causality(ledger_path)
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, result.event_hash)
            contents = ledger_path.read_text(encoding="utf-8")
            ledger_path.write_text(
                contents.replace('"status": "pass"', '"status": "fail"', 1),
                encoding="utf-8",
            )
            self.assertFalse(runtime.ledger.verify_chain())

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.STOP)
            self.assertIn("hash chain", " ".join(completion.reasons))

    def test_verification_survives_ledger_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Causality(root / "ledger.jsonl")
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, result.event_hash)
            self.assertIsNotNone(runtime.ledger.rotate())

            self.assertEqual(runtime.complete(contract).decision, GateDecision.PASS)
            self.assertTrue(runtime.ledger.verify_chain())

    def test_deleted_current_segment_after_rotation_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger_path = root / "ledger.jsonl"
            runtime = Causality(ledger_path)
            contract = _contract(runtime, _requirement())
            result = runtime.verify_requirement(contract, "unit", root=root)
            _cite(runtime, contract, result.event_hash)
            self.assertIsNotNone(runtime.ledger.rotate())
            tools = ToolAdapter(
                runtime.ledger,
                ExecutionAdapter(runtime, contract),
                root=root,
                runner=lambda _argv: CommandResult(0, "", ""),
            )
            tools.run(["noop"])
            self.assertEqual(runtime.complete(contract).decision, GateDecision.REPAIR)
            ledger_path.unlink()

            completion = runtime.complete(contract)

            self.assertEqual(completion.decision, GateDecision.STOP)
            self.assertIn("hash chain", " ".join(completion.reasons))


if __name__ == "__main__":
    unittest.main()
