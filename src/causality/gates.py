from __future__ import annotations

import stat
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .contracts import (
    AuditEventType,
    GateDecision,
    GoalContract,
    IRREVERSIBLE_ACTIONS,
    Risk,
    StateTransition,
    VerificationRequirement,
    VerifierDecision,
    contract_binding_payload,
)
from .durable import file_lock
from .ledger import EvidenceLedger, LedgerEvent, sha256_file


@dataclass(frozen=True)
class GateResult:
    decision: GateDecision
    reasons: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision == GateDecision.PASS

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "reasons": list(self.reasons),
            "allowed": self.allowed,
        }


class HITLGate:
    """Policy gate for plan approval, action execution, and completion."""

    def __init__(self, ledger: EvidenceLedger):
        self.ledger = ledger

    def evaluate_plan(self, contract: GoalContract) -> GateResult:
        snapshot, issue = self._durable_binding(contract)
        if issue is not None:
            return issue
        assert snapshot is not None
        if snapshot.approval_required and not self._has_human_approval(contract.goal_id, "plan"):
            return self._record(
                contract,
                GateDecision.ESCALATE,
                "high-risk plan requires human approval",
            )
        return self._record(contract, GateDecision.PASS, "plan may proceed")

    def can_execute_action(self, contract: GoalContract, action_kind: str) -> GateResult:
        snapshot, issue = self._durable_binding(contract)
        if issue is not None:
            return issue
        assert snapshot is not None
        if snapshot.approval_required and not self._has_human_approval(
            contract.goal_id,
            "plan",
        ):
            return self._record(
                contract,
                GateDecision.ESCALATE,
                "high-risk plan requires human approval before actions",
            )
        action_requires_approval = (
            snapshot.risk_value == Risk.IRREVERSIBLE.value
            or action_kind in IRREVERSIBLE_ACTIONS
        )
        if action_requires_approval and not self._has_human_approval(contract.goal_id, action_kind):
            return self._record(
                contract,
                GateDecision.ESCALATE,
                f"action '{action_kind}' requires human approval",
            )
        return self._record(contract, GateDecision.PASS, f"action '{action_kind}' may execute")

    def _durable_binding(
        self,
        contract: GoalContract,
    ) -> tuple[GoalContract | None, GateResult | None]:
        if not self.ledger.verify_chain():
            return None, GateResult(
                GateDecision.STOP,
                ("ledger hash chain verification failed",),
            )
        payload = self.ledger.contract_snapshot(contract.goal_id)
        if payload is None:
            return None, self._record(
                contract,
                GateDecision.REPAIR,
                "durable contract snapshot is missing",
            )
        snapshot = GoalContract.from_mapping(payload)
        if contract_binding_payload(contract) != contract_binding_payload(snapshot):
            return None, self._record(
                contract,
                GateDecision.REPAIR,
                "live contract differs from durable contract snapshot",
            )
        return snapshot, None

    def complete(
        self,
        contract: GoalContract,
        verifier_decisions: Iterable[VerifierDecision] | None = None,
        *,
        min_passes: int = 2,
        require_evidence: bool = False,
        event_metadata: Mapping[str, object] | None = None,
    ) -> GateResult:
        with file_lock(self.ledger.path):
            return self._complete_locked(
                contract,
                verifier_decisions,
                min_passes=min_passes,
                require_evidence=require_evidence,
                event_metadata=event_metadata,
            )

    def unmet_verification_ids(
        self,
        contract: GoalContract,
        events: list[LedgerEvent],
    ) -> tuple[str, ...]:
        """Return required IDs that do not satisfy the completion freshness rules.

        This is the read-only subset of the structured completion gate.  The
        caller supplies the frozen contract and its already chain-verified,
        task-scoped events so a status query never records a gate decision.
        """

        last_mutation = max(
            (
                index
                for index, event in enumerate(events)
                if event.payload.get("mutates_task") is True
            ),
            default=-1,
        )
        return tuple(
            requirement.id
            for requirement in contract.verification_requirements
            if requirement.required
            and self._structured_requirement_issues(
                (requirement,),
                events,
                workspace_root=contract.workspace_root,
                last_mutation=last_mutation,
            )[0]
        )

    def _complete_locked(
        self,
        contract: GoalContract,
        verifier_decisions: Iterable[VerifierDecision] | None = None,
        *,
        min_passes: int = 2,
        require_evidence: bool = False,
        event_metadata: Mapping[str, object] | None = None,
    ) -> GateResult:
        snapshot, issue = self._durable_binding(contract)
        if issue is not None:
            return issue
        assert snapshot is not None
        events: list[LedgerEvent] | None = None
        if snapshot.approval_required:
            events = self.ledger.events_for_contract(
                contract.goal_id,
                all_segments=True,
            )
            plan_issue = self._plan_approval_issue(events)
            if plan_issue:
                return self._record(
                    contract,
                    GateDecision.ESCALATE,
                    plan_issue,
                    metadata=event_metadata,
                )
        structured = snapshot.verification_requirements
        if structured or snapshot.required_evidence_kinds() or snapshot.approval_required:
            if events is None:
                events = self.ledger.events_for_contract(
                    contract.goal_id,
                    all_segments=True,
                )
            last_mutation = max(
                (
                    index
                    for index, event in enumerate(events)
                    if event.payload.get("mutates_task") is True
                ),
                default=-1,
            )
            if structured:
                issues, review_after, requirement_hashes = (
                    self._structured_requirement_issues(
                        structured,
                        events,
                        workspace_root=snapshot.workspace_root,
                        last_mutation=last_mutation,
                    )
                )
            else:
                issues, review_after, requirement_hashes = [], last_mutation, set()
            generic_issues, generic_after, generic_hashes = (
                self._structured_generic_evidence_issues(
                    snapshot,
                    events,
                    workspace_root=snapshot.workspace_root,
                    last_mutation=last_mutation,
                )
            )
            issues.extend(generic_issues)
            review_after = max(review_after, generic_after)
            requirement_hashes.update(generic_hashes)
            blocked = bool(
                structured
                and self._current_state(events) == StateTransition.BLOCKED.value
            )
            if blocked:
                issues.append("task is blocked; resolve it before completion")
            issues.extend(
                self._structured_verifier_issues(
                    events,
                    verifier_decisions,
                    review_after=review_after,
                    requirement_hashes=requirement_hashes,
                    min_passes=max(2, min_passes),
                )
            )
            if issues:
                return self._record(
                    contract,
                    GateDecision.ESCALATE if blocked else GateDecision.REPAIR,
                    issues,
                    metadata=event_metadata,
                )

            final_issue = (
                self._structured_final_approval_issue(
                    events,
                    review_after=review_after,
                    requirement_hashes=requirement_hashes,
                )
                if snapshot.approval_required
                else None
            )
            if final_issue:
                return self._record(
                    contract,
                    GateDecision.ESCALATE,
                    final_issue,
                    metadata=event_metadata,
                )
            return self._record(
                contract,
                GateDecision.PASS,
                "completion criteria satisfied",
                metadata=event_metadata,
            )

        records = (
            list(verifier_decisions)
            if verifier_decisions is not None
            else self._verifier_decisions(contract.goal_id)
        )
        # A verifier's verdict is its LATEST decision. Counting raw events let a
        # single verifier passing in two loop iterations satisfy the
        # "independent passes" rule, and let an already-fixed critical failure
        # from an earlier iteration block completion forever (code review
        # 2026-06-13, F1/F2).
        latest: dict[str, VerifierDecision] = {}
        for item in records:
            key = item.verifier.strip().casefold() if isinstance(item.verifier, str) else ""
            if key:
                latest[key] = item
        verdicts = list(latest.values())

        if any(item.is_critical_failure for item in verdicts):
            return self._record(
                contract,
                GateDecision.REPAIR,
                "critical verifier failure remains unresolved",
                metadata=event_metadata,
            )

        # A pass counts only if it is substantive (cites evidence or a rationale)
        # -- a hollow rubber-stamp must not satisfy the quorum (code review
        # 2026-06-13: verifier substance unchecked). ``require_evidence`` raises
        # the bar to an explicit evidence_ref for high-assurance completion.
        pass_count = sum(
            1 for item in verdicts if item.counts_as_pass(require_evidence=require_evidence)
        )
        min_passes = max(2, min_passes)
        if pass_count < min_passes:
            hollow = sorted(
                item.verifier
                for item in verdicts
                if item.is_pass and not item.counts_as_pass(require_evidence=require_evidence)
            )
            reason = (
                f"completion requires at least {min_passes} substantive independent "
                f"verifier passes"
            )
            if hollow:
                reason += "; unsubstantiated passes ignored: " + ", ".join(hollow)
            return self._record(
                contract,
                GateDecision.REPAIR,
                reason,
                metadata=event_metadata,
            )

        missing = self._missing_required_evidence(snapshot)
        if missing:
            return self._record(
                contract,
                GateDecision.REPAIR,
                "missing required evidence: " + ", ".join(sorted(missing)),
                metadata=event_metadata,
            )

        return self._record(
            contract,
            GateDecision.PASS,
            "completion criteria satisfied",
            metadata=event_metadata,
        )

    @staticmethod
    def _current_state(events: list[LedgerEvent]) -> str | None:
        state = None
        for event in events:
            if event.event_type == AuditEventType.STATE_TRANSITION.value:
                state = event.payload.get("state")
        return state

    @staticmethod
    def _plan_approval_issue(events: list[LedgerEvent]) -> str | None:
        decisions = [
            (index, event)
            for index, event in enumerate(events)
            if event.event_type == AuditEventType.HUMAN_DECISION.value
            and event.payload.get("stage") == "plan"
        ]
        if not decisions:
            return "high-risk plan approval is required"
        _, latest = decisions[-1]
        approver = latest.payload.get("approver")
        rationale = latest.payload.get("rationale")
        if (
            latest.payload.get("approved") is not True
            or not isinstance(approver, str)
            or not approver.strip()
            or not isinstance(rationale, str)
            or not rationale.strip()
        ):
            return "high-risk plan requires current human approval"

        plan_approved = False
        for event in events:
            if (
                event.event_type == AuditEventType.HUMAN_DECISION.value
                and event.payload.get("stage") == "plan"
            ):
                plan_approved = bool(
                    event.payload.get("approved") is True
                    and str(event.payload.get("approver", "")).strip()
                    and str(event.payload.get("rationale", "")).strip()
                )
                continue
            is_work = (
                event.event_type
                in {
                    AuditEventType.TOOL_CALL.value,
                    AuditEventType.BROWSER_ACTION.value,
                }
                or (
                    event.event_type == AuditEventType.HUMAN_DECISION.value
                    and event.payload.get("manual") is True
                    and str(event.payload.get("stage", "")).startswith("verification:")
                )
                or event.payload.get("mutates_task") is True
            )
            if is_work and not plan_approved:
                return "plan approval must be active at every task work event"
        return None

    def _structured_generic_evidence_issues(
        self,
        contract: GoalContract,
        events: list[LedgerEvent],
        *,
        workspace_root: str,
        last_mutation: int,
    ) -> tuple[list[str], int, set[str]]:
        issues: list[str] = []
        review_after = -1
        hashes: set[str] = set()
        required_kinds = sorted(contract.required_evidence_kinds())
        current_workspace: str | None = None
        workspace_error: str | None = None
        if required_kinds:
            from .verification import workspace_fingerprint, workspace_fingerprint_digest

            try:
                current_workspace = workspace_fingerprint_digest(
                    workspace_fingerprint(Path(workspace_root), self.ledger.path)
                )
            except OSError as exc:
                workspace_error = type(exc).__name__
        for kind in required_kinds:
            matches = [
                (index, event)
                for index, event in enumerate(events)
                if event.event_type == AuditEventType.EVIDENCE.value
                and event.payload.get("kind") == kind
            ]
            if not matches:
                issues.append(f"missing required generic evidence kind: {kind}")
                continue
            index, event = matches[-1]
            review_after = max(review_after, index)
            valid = True
            if index < last_mutation:
                issues.append(f"stale generic evidence kind: {kind}")
                valid = False
            if workspace_error is not None:
                issues.append(
                    f"generic evidence workspace is unreadable: {workspace_error}"
                )
                valid = False
            elif event.payload.get("evidence_workspace_fingerprint_sha256") != current_workspace:
                issues.append(f"stale generic evidence workspace kind: {kind}")
                valid = False
            if valid:
                hashes.add(event.entry_hash)
        return issues, review_after, hashes

    def _structured_requirement_issues(
        self,
        requirements: tuple[VerificationRequirement, ...],
        events: list[LedgerEvent],
        *,
        workspace_root: str,
        last_mutation: int,
    ) -> tuple[list[str], int, set[str]]:
        """Validate latest per-ID results and return the review batch boundary."""
        issues: list[str] = []
        evidence_by_hash = {
            event.entry_hash: (index, event)
            for index, event in enumerate(events)
            if event.event_type == AuditEventType.EVIDENCE.value
        }
        tool_by_hash = {
            event.entry_hash: (index, event)
            for index, event in enumerate(events)
            if event.event_type == AuditEventType.TOOL_CALL.value
        }
        review_after = last_mutation
        requirement_hashes: set[str] = set()

        for requirement in requirements:
            if not requirement.required:
                continue
            if requirement.manual:
                matches = [
                    (index, event)
                    for index, event in enumerate(events)
                    if event.event_type == AuditEventType.HUMAN_DECISION.value
                    and event.payload.get("stage") == f"verification:{requirement.id}"
                    and event.payload.get("manual") is True
                ]
                if not matches:
                    issues.append(f"{requirement.id}: missing manual human verdict")
                    continue
                index, decision = matches[-1]
                review_after = max(review_after, index)
                if index <= last_mutation:
                    issues.append(f"{requirement.id}: stale manual verdict")
                if decision.payload.get("approved") is not True:
                    issues.append(f"{requirement.id}: manual verdict is not approved")
                approver = decision.payload.get("approver")
                if not isinstance(approver, str) or not approver.strip():
                    issues.append(f"{requirement.id}: manual verdict approver is blank")
                evidence_hash = decision.payload.get("evidence_hash")
                cited = evidence_by_hash.get(evidence_hash)
                if not isinstance(evidence_hash, str) or not evidence_hash.strip() or cited is None:
                    issues.append(f"{requirement.id}: manual verdict has invalid evidence hash")
                elif cited[0] < last_mutation:
                    issues.append(f"{requirement.id}: manual evidence is stale")
                else:
                    evidence_workspace = cited[1].payload.get(
                        "evidence_workspace_fingerprint_sha256"
                    )
                    decision_workspace = decision.payload.get(
                        "workspace_fingerprint_sha256"
                    )
                    if (
                        not isinstance(evidence_workspace, str)
                        or not evidence_workspace.strip()
                        or evidence_workspace != decision_workspace
                    ):
                        issues.append(
                            f"{requirement.id}: manual evidence workspace differs "
                            "from verdict"
                        )
                    else:
                        requirement_hashes.add(evidence_hash)
                workspace_issues = self._workspace_issues(
                    requirement,
                    decision,
                    workspace_root=workspace_root,
                )
                issues.extend(
                    f"{requirement.id}: {issue}" for issue in workspace_issues
                )
                continue

            matches = [
                (index, event)
                for index, event in enumerate(events)
                if event.event_type == AuditEventType.EVIDENCE.value
                and event.payload.get("kind") == "verification_result"
                and event.payload.get("requirement_id") == requirement.id
                and event.payload.get("manual") is False
            ]
            if not matches:
                issues.append(f"{requirement.id}: missing verification result")
                continue
            index, result = matches[-1]
            review_after = max(review_after, index)
            valid = True
            if index <= last_mutation:
                issues.append(f"{requirement.id}: verification result is stale")
                valid = False
            if result.payload.get("status") != "pass":
                issues.append(
                    f"{requirement.id}: latest verification status is "
                    f"{result.payload.get('status', 'missing')}"
                )
                valid = False
            if tuple(result.payload.get("argv", ())) != requirement.argv:
                issues.append(f"{requirement.id}: recorded argv differs from contract")
                valid = False
            if (
                tuple(result.payload.get("expected_exit_codes", ()))
                != requirement.expected_exit_codes
            ):
                issues.append(
                    f"{requirement.id}: recorded expected exit codes differ from contract"
                )
                valid = False
            if result.payload.get("exit_code") not in requirement.expected_exit_codes:
                issues.append(f"{requirement.id}: exit code did not satisfy contract")
                valid = False
            if not str(result.payload.get("completed_at", "")).strip():
                issues.append(f"{requirement.id}: completion timestamp is missing")
                valid = False

            tool_record = tool_by_hash.get(result.payload.get("tool_event_hash"))
            if tool_record is None:
                issues.append(f"{requirement.id}: executable tool provenance is missing")
                valid = False
            else:
                tool_index, tool_event = tool_record
                tool_payload = tool_event.payload
                own_mutation_hash = result.payload.get("artifact_mutation_event_hash")
                own_mutation = events[last_mutation] if last_mutation >= 0 else None
                tool_precedes_own_artifact_mutation = bool(
                    own_mutation
                    and own_mutation.entry_hash == own_mutation_hash
                    and own_mutation.payload.get("requirement_id") == requirement.id
                    and own_mutation.payload.get("caused_by_tool_event_hash")
                    == result.payload.get("tool_event_hash")
                    and last_mutation < index
                )
                if (
                    tool_index >= index
                    or (
                        tool_index <= last_mutation
                        and not tool_precedes_own_artifact_mutation
                    )
                ):
                    issues.append(f"{requirement.id}: executable tool provenance is stale")
                    valid = False
                output_fields = (
                    "stdout",
                    "stderr",
                    "stdout_bytes",
                    "stderr_bytes",
                    "stdout_sha256",
                    "stderr_sha256",
                    "stdout_truncated",
                    "stderr_truncated",
                )
                if (
                    tuple(tool_payload.get("argv", ())) != requirement.argv
                    or tool_payload.get("exit_code") != result.payload.get("exit_code")
                    or any(
                        field not in tool_payload
                        or field not in result.payload
                        or tool_payload.get(field) != result.payload.get(field)
                        for field in output_fields
                    )
                    or tool_payload.get("mutates_task") is not False
                    or tool_payload.get("environment_overrides")
                    != {"PYTHONDONTWRITEBYTECODE": "1"}
                ):
                    issues.append(f"{requirement.id}: tool result differs from evidence")
                    valid = False

            workspace_issues = self._workspace_issues(
                requirement,
                result,
                workspace_root=workspace_root,
            )
            if workspace_issues:
                issues.extend(f"{requirement.id}: {issue}" for issue in workspace_issues)
                valid = False
            artifact_issues = self._artifact_issues(
                requirement,
                result,
                workspace_root=workspace_root,
            )
            if artifact_issues:
                issues.extend(f"{requirement.id}: {issue}" for issue in artifact_issues)
                valid = False
            if not result.entry_hash.strip():
                issues.append(f"{requirement.id}: result event hash is blank")
                valid = False
            if valid:
                requirement_hashes.add(result.entry_hash)

        return issues, review_after, requirement_hashes

    def _workspace_issues(
        self,
        requirement: VerificationRequirement,
        event: LedgerEvent,
        *,
        workspace_root: str,
    ) -> list[str]:
        from .verification import workspace_fingerprint, workspace_fingerprint_digest

        recorded = event.payload.get("workspace_fingerprint_sha256")
        if not isinstance(recorded, str) or not recorded.strip():
            return ["workspace fingerprint is missing"]
        try:
            current = workspace_fingerprint_digest(
                workspace_fingerprint(
                    Path(workspace_root),
                    self.ledger.path,
                    requirement.artifact_paths,
                )
            )
        except OSError as exc:
            return [f"workspace fingerprint is unreadable: {type(exc).__name__}"]
        return [] if current == recorded else ["workspace changed after verification"]

    @staticmethod
    def _artifact_issues(
        requirement: VerificationRequirement,
        event: LedgerEvent,
        *,
        workspace_root: str,
    ) -> list[str]:
        issues: list[str] = []
        records = {
            record.get("path"): record
            for record in event.payload.get("artifact_records", [])
            if isinstance(record, dict)
        }
        ledger_artifacts = {
            item.get("path"): item for item in event.artifacts if isinstance(item, dict)
        }
        root = Path(workspace_root).resolve()
        for declared_path, expected in requirement.artifact_paths.items():
            record = records.get(declared_path)
            if record is None:
                issues.append(f"artifact record missing: {declared_path}")
                continue
            if record.get("expected_sha256") != expected:
                issues.append(f"artifact expectation differs from contract: {declared_path}")
            actual = record.get("actual_sha256")
            resolved = str(record.get("resolved_path", ""))
            if not actual or not resolved:
                issues.append(f"artifact missing: {declared_path}")
                continue
            ledger_record = ledger_artifacts.get(resolved)
            if (
                ledger_record is None
                or ledger_record.get("sha256") != actual
                or ledger_record.get("file_type") != record.get("file_type")
                or ledger_record.get("mode") != record.get("mode")
            ):
                issues.append(f"artifact ledger hash mismatch: {declared_path}")
                continue
            candidate = Path(declared_path)
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                metadata = candidate.lstat()
                current_resolved = candidate.resolve()
                current_mode = stat.S_IMODE(metadata.st_mode)
            except (OSError, RuntimeError):
                issues.append(f"artifact changed after verification: {declared_path}")
                continue
            if not current_resolved.is_relative_to(root):
                issues.append(f"artifact escapes project root: {declared_path}")
                continue
            if str(current_resolved) != resolved:
                issues.append(f"artifact resolved path changed: {declared_path}")
            if not stat.S_ISREG(metadata.st_mode) or record.get("file_type") != "file":
                issues.append(f"artifact is not a regular file: {declared_path}")
                continue
            if current_mode != record.get("mode"):
                issues.append(f"artifact mode changed after verification: {declared_path}")
            try:
                current_hash = sha256_file(current_resolved)
            except OSError:
                current_hash = None
            if current_hash != actual:
                issues.append(f"artifact changed after verification: {declared_path}")
            elif expected is not None and actual != expected:
                issues.append(f"artifact hash mismatch: {declared_path}")
        return issues

    def _structured_verifier_issues(
        self,
        events: list[LedgerEvent],
        supplied: Iterable[VerifierDecision] | None,
        *,
        review_after: int,
        requirement_hashes: set[str],
        min_passes: int,
    ) -> list[str]:
        issues: list[str] = []
        if supplied is not None:
            issues.append("structured completion accepts only ledger-recorded verifier decisions")
            records: list[VerifierDecision] = []
        else:
            records = [
                self._decision_from_event(event)
                for index, event in enumerate(events)
                if index > review_after
                and event.event_type == AuditEventType.VERIFIER_DECISION.value
            ]

        invalid_names = [item.verifier for item in records if not self._verifier_key(item)]
        if invalid_names:
            issues.append("verifier identity must be non-blank")
        duplicates = sorted(
            name
            for name, count in Counter(
                self._verifier_key(item)
                for item in records
                if self._verifier_key(item)
            ).items()
            if count > 1
        )
        if duplicates:
            issues.append(
                "duplicate verifier names in current review attempt: " + ", ".join(duplicates)
            )

        verdicts = records
        if any(item.is_critical_failure for item in verdicts):
            issues.append("critical verifier failure remains unresolved")

        valid_passes = 0
        invalid_citations: list[str] = []
        for item in verdicts:
            if (
                not item.is_pass
                or not self._verifier_key(item)
            ):
                continue
            refs = tuple(item.evidence_refs)
            valid = (
                bool(refs)
                and all(isinstance(ref, str) and ref.strip() for ref in refs)
                and set(refs) == requirement_hashes
            )
            if valid:
                valid_passes += 1
            else:
                invalid_citations.append(item.verifier)
        if invalid_citations:
            issues.append(
                "invalid task-scoped verification citation: "
                + ", ".join(sorted(invalid_citations))
            )
        if valid_passes < min_passes:
            issues.append(
                f"completion requires at least {min_passes} independent cited verifier passes"
            )
        return issues

    @staticmethod
    def _verifier_key(decision: VerifierDecision) -> str:
        if not isinstance(decision.verifier, str):
            return ""
        return decision.verifier.strip().casefold()

    @staticmethod
    def _structured_final_approval_issue(
        events: list[LedgerEvent],
        *,
        review_after: int,
        requirement_hashes: set[str],
    ) -> str | None:
        verdict_end = max(
            (
                index
                for index, event in enumerate(events)
                if index > review_after
                and event.event_type == AuditEventType.VERIFIER_DECISION.value
            ),
            default=review_after,
        )
        decisions = [
            (index, event)
            for index, event in enumerate(events)
            if event.event_type == AuditEventType.HUMAN_DECISION.value
            and event.payload.get("stage") == "final"
        ]
        if not decisions:
            return "final human approval required for high-risk contract"
        index, decision = decisions[-1]
        refs = tuple(decision.payload.get("evidence_refs", ()))
        if (
            index <= verdict_end
            or decision.payload.get("approved") is not True
            or not str(decision.payload.get("approver", "")).strip()
            or not refs
            or any(not isinstance(ref, str) or not ref.strip() for ref in refs)
            or set(refs) != requirement_hashes
        ):
            return "final approval must follow current review and cite current evidence"
        return None

    @staticmethod
    def _decision_from_event(event: LedgerEvent) -> VerifierDecision:
        payload = event.payload
        return VerifierDecision(
            verifier=payload.get("verifier", "unknown"),
            status=payload.get("status", "fail"),
            rationale=payload.get("rationale", ""),
            severity=payload.get("severity", "normal"),
            evidence_refs=tuple(payload.get("evidence_refs", ())),
            created_at=payload.get("created_at", event.timestamp),
        )

    def check_tool_allowed(self, contract: GoalContract, tool: str) -> GateResult:
        """Enforce the Allowed-tools clause (ADR 0001 §2.3).

        An empty ``allowed_tools`` means no restriction was declared, so any
        tool passes. When a restriction is declared, a tool outside it
        escalates so a human can decide whether to widen scope.
        """
        snapshot, binding_issue = self._durable_binding(contract)
        if binding_issue is not None:
            return binding_issue
        assert snapshot is not None
        allowed = snapshot.permissions.allowed_tools
        if allowed and tool not in allowed:
            return self._record(
                contract,
                GateDecision.ESCALATE,
                f"tool '{tool}' is outside the contract's allowed_tools",
            )
        return self._record(contract, GateDecision.PASS, f"tool '{tool}' is permitted")

    def check_non_goal(self, contract: GoalContract, action_desc: str) -> GateResult:
        """Enforce the Non-goals clause (ADR 0001 §2.3).

        A non-goal is a hard boundary, so a match stops the action rather than
        escalating: the contract has already declared this work out of scope.
        """
        snapshot, binding_issue = self._durable_binding(contract)
        if binding_issue is not None:
            return binding_issue
        assert snapshot is not None
        text = action_desc.lower()
        for non_goal in snapshot.non_goals:
            needle = non_goal.strip().lower()
            if needle and needle in text:
                return self._record(
                    contract,
                    GateDecision.STOP,
                    f"action conflicts with declared non-goal: {non_goal}",
                )
        return self._record(contract, GateDecision.PASS, "action does not hit a non-goal")

    def check_network_scope(self, contract: GoalContract, origin: str) -> GateResult:
        """Require an exact origin declared by the frozen contract."""
        snapshot, binding_issue = self._durable_binding(contract)
        if binding_issue is not None:
            return binding_issue
        assert snapshot is not None
        if not isinstance(origin, str) or not origin.strip():
            return self._record(
                contract,
                GateDecision.STOP,
                "network origin must be non-blank",
            )
        if origin not in snapshot.permissions.network_scope:
            return self._record(
                contract,
                GateDecision.STOP,
                f"network origin is outside the contract scope: {origin}",
            )
        return self._record(contract, GateDecision.PASS, f"network origin is permitted: {origin}")

    def check_auth_scope(
        self,
        contract: GoalContract,
        auth_ref: str | None,
    ) -> GateResult:
        """Allow anonymous access or an exact server-owned credential alias."""
        snapshot, binding_issue = self._durable_binding(contract)
        if binding_issue is not None:
            return binding_issue
        assert snapshot is not None
        if auth_ref is None:
            return self._record(contract, GateDecision.PASS, "anonymous access is permitted")
        if not isinstance(auth_ref, str) or not auth_ref.strip():
            return self._record(
                contract,
                GateDecision.STOP,
                "auth_ref must be non-blank when provided",
            )
        if auth_ref not in snapshot.permissions.auth_scope:
            return self._record(
                contract,
                GateDecision.STOP,
                f"credential alias is outside the contract scope: {auth_ref}",
            )
        return self._record(contract, GateDecision.PASS, f"credential alias is permitted: {auth_ref}")

    def should_stop(
        self,
        contract: GoalContract,
        iteration_state: Mapping[str, int],
        *,
        event_metadata: Mapping[str, object] | None = None,
    ) -> GateResult:
        """Enforce the Stop-condition clause by reading ``stopping_policy``.

        This is the consumer the policy previously lacked (ADR 0006 C-STOP-1).
        ``iteration_state`` is supplied by the loop runtime. Hitting the
        iteration or no-progress ceiling stops; exhausting failed hypotheses
        escalates (consistent with the root-cause protocol).

        The loop polls this *before every iteration*, so a non-terminal "keep
        going" result is a pure query and is deliberately NOT recorded:
        appending a GATE_DECISION on each poll would flood the ledger with one
        event per iteration and inflate Reflect's ``gate_counts[pass]`` with
        observer noise -- the act of checking the stop condition would change
        the very trail Reflect later distills (a should_stop observer effect).
        Only a terminal STOP/ESCALATE is a material decision worth a record.
        """
        snapshot, binding_issue = self._durable_binding(contract)
        if binding_issue is not None:
            return binding_issue
        assert snapshot is not None
        policy = snapshot.stopping_policy
        iterations = int(iteration_state.get("iterations", 0))
        no_progress = int(iteration_state.get("no_progress_iterations", 0))
        failed = int(iteration_state.get("failed_hypotheses", 0))

        max_iterations = int(policy.get("max_iterations", 0) or 0)
        max_no_progress = int(policy.get("no_progress_iterations", 0) or 0)
        max_failed = int(policy.get("max_failed_hypotheses", 0) or 0)

        if max_iterations and iterations >= max_iterations:
            return self._record(
                contract,
                GateDecision.STOP,
                f"reached max_iterations ({max_iterations})",
                metadata=event_metadata,
            )
        if max_no_progress and no_progress >= max_no_progress:
            return self._record(
                contract,
                GateDecision.STOP,
                f"no progress for {max_no_progress} iteration(s)",
                metadata=event_metadata,
            )
        if max_failed and failed >= max_failed:
            return self._record(
                contract,
                GateDecision.ESCALATE,
                f"reached max_failed_hypotheses ({max_failed})",
                metadata=event_metadata,
            )
        # Keep going: a pure poll, not a material gate decision. Return without
        # recording so the per-iteration check leaves no observer footprint in
        # the ledger. The loop inspects only the decision value.
        return GateResult(GateDecision.PASS, ("stop condition not met",))

    def _missing_required_evidence(self, contract: GoalContract) -> set[str]:
        required = contract.required_evidence_kinds()
        if not required:
            return set()
        seen = {
            event.payload.get("kind")
            for event in self.ledger.events_for_contract(
                contract.goal_id,
                all_segments=True,
            )
            if event.event_type == AuditEventType.EVIDENCE.value
        }
        return required - seen

    def _has_human_approval(self, contract_id: str, stage: str) -> bool:
        decisions = [
            event
            for event in self.ledger.events_for_contract(contract_id, all_segments=True)
            if event.event_type == AuditEventType.HUMAN_DECISION.value
            and event.payload.get("stage") == stage
        ]
        if not decisions:
            return False
        latest = decisions[-1].payload
        approver = latest.get("approver")
        rationale = latest.get("rationale")
        return bool(
            latest.get("approved") is True
            and isinstance(approver, str)
            and approver.strip()
            and isinstance(rationale, str)
            and rationale.strip()
        )

    def _verifier_decisions(self, contract_id: str) -> list[VerifierDecision]:
        decisions: list[VerifierDecision] = []
        for event in self.ledger.events_for_contract(contract_id, all_segments=True):
            if event.event_type == AuditEventType.VERIFIER_DECISION.value:
                decisions.append(self._decision_from_event(event))
        return decisions

    def _record(
        self,
        contract: GoalContract,
        decision: GateDecision,
        reason: str | Iterable[str],
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> GateResult:
        reasons = (reason,) if isinstance(reason, str) else tuple(reason)
        result = GateResult(decision=decision, reasons=reasons)
        payload = result.to_dict()
        if metadata:
            overlap = payload.keys() & metadata.keys()
            if overlap:
                raise ValueError(
                    "gate metadata cannot replace authoritative fields: "
                    + ", ".join(sorted(overlap))
                )
            payload.update(metadata)
        self.ledger.append(
            AuditEventType.GATE_DECISION,
            payload,
            contract_id=contract.goal_id,
        )
        return result
