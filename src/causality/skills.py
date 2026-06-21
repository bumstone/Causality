"""Earned-skill distiller, reproducibility tracking, and HITL promotion.

This is the back half of the evolution loop (ADR 0005 §2.4, ADR 0006 §6.1-4):
a *rewarded trajectory* recorded in the EvidenceLedger is distilled into a
reusable **earned skill** candidate, its procedure quality is measured by
**n-of-m reproducibility** (multiple successes across attempts, so a single
lucky/flaky path is not promoted), and it only enters the library through an
explicit **HITL promotion gate** after being **deduped against authored**
skills (gstack/Superpowers playbooks) to prevent skill explosion.

Candidates and promoted skills are appended as JSONL under
``<root>/skills/candidates.jsonl`` and ``<root>/skills/promoted.jsonl`` so the
store mirrors the on-demand layout the bootstrap installs (ADR 0007), matching
the ``memory.py`` style. ``candidates()`` returns the latest authoritative
state per ``skill_id`` (the file is rewritten on each outcome update).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .contracts import GoalContract
from .durable import DurableJsonl
from .ledger import EvidenceLedger, LedgerEvent


# Tiny stop set so recall matches on contentful tokens, not glue words.
_STOPWORDS = frozenset(
    {"the", "and", "for", "with", "that", "this", "into", "from", "your", "our"}
)


def _tokens(text: str) -> frozenset[str]:
    """Content tokens of ``text``: lowercased alphanumerics, length >= 3, no stopwords."""
    return frozenset(
        word
        for word in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(word) >= 3 and word not in _STOPWORDS
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Token-set Jaccard overlap; 0 when either side is empty."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


# A distilled step is shared and persisted, so payload values are redacted and
# bounded before they enter a reusable skill: a key that looks like a secret is
# masked and every value is truncated. The ledger still holds the raw payload --
# this is defense-in-depth so a promoted/shared skill never copies a secret out
# of the ledger.
_SENSITIVE_KEY = re.compile(
    r"secret|token|password|passwd|credential|api[_-]?key|auth|cookie|bearer|session",
    re.IGNORECASE,
)
# Well-known secret *shapes* redacted even under a benign key, since key-name
# matching alone misses a secret in value position (e.g. {"output": "sk-..."}).
# Token bodies allow ``-``/``_`` so current variants (sk-proj-..., sk-svcacct-...)
# are caught; each token alternative carries its own \b so an embedded prefix in
# an ordinary word (e.g. "task-management-system") does not false-positive.
_SECRET_VALUE = re.compile(
    r"\bsk-[A-Za-z0-9_-]{16,}"
    r"|\bgh[opsu]_[A-Za-z0-9]{20,}"
    r"|\bAKIA[0-9A-Z]{12,}"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}"
    r"|\bAIza[0-9A-Za-z_-]{20,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
# Bulky/non-procedural fields dropped so a step stays a compact recipe, not a dump.
_SKIP_KEYS = frozenset(
    {"diff", "state_hash", "created_at", "evidence_refs", "rationale", "reasons", "summary"}
)
_MAX_VALUE_LEN = 80
_MAX_ARGS = 6
_TOOL_KEYS = ("tool", "verifier", "command", "type", "action")
_OUTCOME_KEYS = ("kind", "status", "decision", "state", "stage")


def _redact_value(key: str, value: Any) -> str:
    """A bounded, secret-safe string for one payload value."""
    if _SENSITIVE_KEY.search(key):
        return "<redacted>"
    text = (
        json.dumps(value, ensure_ascii=True, sort_keys=True)
        if isinstance(value, (dict, list))
        else str(value)
    )
    # Mask a secret-shaped value even under a benign key (also catches a secret
    # nested inside a dict/list value, which was serialized above).
    if _SECRET_VALUE.search(text):
        return "<redacted>"
    return text if len(text) <= _MAX_VALUE_LEN else text[: _MAX_VALUE_LEN - 3] + "..."


def _first_present(payload: Mapping[str, Any], keys: Sequence[str]) -> tuple[str, str]:
    """First ``(key, str(value))`` whose key is present and truthy in payload."""
    for key in keys:
        if payload.get(key) not in (None, ""):
            return key, str(payload[key])
    return "", ""


def _artifact_id(record: Mapping[str, Any]) -> str:
    """Stable artifact identity ``path@sha256[:12]`` (or ``path`` if unhashed)."""
    path = str(record.get("path", ""))
    digest = record.get("sha256")
    return f"{path}@{str(digest)[:12]}" if digest else path


@dataclass(frozen=True)
class SkillStep:
    """One reproducible step of a distilled procedure.

    Richer than the old ``"event_type:kind"`` string: it binds the *tool/command*
    the step used, its salient *args*, any *artifacts* it produced, and the
    *outcome* -- so a recalled skill is an actionable recipe, not just a shape.
    """

    action: str
    tool: str = ""
    args: tuple[tuple[str, str], ...] = ()
    artifacts: tuple[str, ...] = ()
    outcome: str = ""

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "tool": self.tool,
            "args": [list(pair) for pair in self.args],
            "artifacts": list(self.artifacts),
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "SkillStep":
        # Back-compat: steps distilled before this change were "event_type:outcome"
        # strings -- wrap them so existing skill files still load.
        if isinstance(value, str):
            action, _, outcome = value.partition(":")
            return cls(action=action, outcome=outcome)
        return cls(
            action=str(value.get("action", "")),
            tool=str(value.get("tool", "")),
            args=tuple((str(k), str(v)) for k, v in value.get("args", ())),
            artifacts=tuple(str(item) for item in value.get("artifacts", ())),
            outcome=str(value.get("outcome", "")),
        )


def _distill_step(event: LedgerEvent) -> SkillStep:
    """Build one reproducible :class:`SkillStep` from a ledger event.

    Names the step's tool/command and outcome from the first matching payload
    key, binds the remaining salient args (redacted + bounded), and records the
    artifacts the event produced (``path@hash``) so reuse can check the same
    outputs -- replacing the old ``event_type:kind`` trace.
    """
    payload: Mapping[str, Any] = event.payload or {}
    tool_key, tool = _first_present(payload, _TOOL_KEYS)
    outcome_key, outcome = _first_present(payload, _OUTCOME_KEYS)
    consumed = {tool_key, outcome_key} | _SKIP_KEYS
    args = tuple(
        (key, _redact_value(key, payload[key]))
        for key in sorted(payload)
        if key and key not in consumed and payload[key] is not None
    )[:_MAX_ARGS]
    artifacts = tuple(
        _artifact_id(record)
        for record in (event.artifacts or ())
        if isinstance(record, Mapping) and record.get("path")
    )
    return SkillStep(
        action=event.event_type, tool=tool, args=args, artifacts=artifacts, outcome=outcome
    )


@dataclass(frozen=True)
class SkillCandidate:
    skill_id: str
    objective: str
    steps: tuple[SkillStep, ...]
    provenance: str | None = None
    attempts: int = 0
    successes: int = 0

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "objective": self.objective,
            "steps": [step.to_dict() for step in self.steps],
            "provenance": self.provenance,
            "attempts": self.attempts,
            "successes": self.successes,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "SkillCandidate":
        return cls(
            skill_id=value["skill_id"],
            objective=value["objective"],
            steps=tuple(SkillStep.from_dict(step) for step in value.get("steps", ())),
            provenance=value.get("provenance"),
            attempts=int(value.get("attempts", 0)),
            successes=int(value.get("successes", 0)),
        )


class SkillPromotionError(ValueError):
    """Raised when distillation/promotion would violate the skill gate rules."""


@dataclass
class SkillStore:
    """Append-only earned-skill store rooted at ``<root>/skills/``."""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def _candidates_path(self) -> Path:
        return self.root / "skills" / "candidates.jsonl"

    def _promoted_path(self) -> Path:
        return self.root / "skills" / "promoted.jsonl"

    def distill(
        self,
        ledger: EvidenceLedger,
        contract: GoalContract,
        *,
        provenance: str | None = None,
    ) -> SkillCandidate:
        """Distill the contract's ledger trajectory into a skill candidate.

        Reads the action sequence for ``contract.goal_id`` (in order) and
        builds the reusable procedure. The candidate starts with no recorded
        attempts; reproducibility is accrued via :meth:`record_outcome`.
        """
        matches = ledger.events_for_contract(contract.goal_id)
        if not matches:
            raise SkillPromotionError(
                f"no ledger events for contract {contract.goal_id!r} to distill"
            )
        steps = tuple(_distill_step(event) for event in matches)
        resolved_provenance = provenance if provenance is not None else matches[-1].entry_hash
        candidate = SkillCandidate(
            skill_id=uuid4().hex,
            objective=contract.title,
            steps=steps,
            provenance=resolved_provenance,
            attempts=0,
            successes=0,
        )
        self._append(self._candidates_path(), candidate)
        return candidate

    def record_outcome(self, skill_id: str, *, success: bool) -> SkillCandidate:
        """Record one reproducibility attempt for a candidate.

        Increments ``attempts`` (and ``successes`` when ``success``) and
        rewrites the candidates file with the updated authoritative state.
        """
        candidates = self._latest_candidates()
        if skill_id not in candidates:
            raise SkillPromotionError(f"unknown skill_id: {skill_id!r}")
        current = candidates[skill_id]
        updated = SkillCandidate(
            skill_id=current.skill_id,
            objective=current.objective,
            steps=current.steps,
            provenance=current.provenance,
            attempts=current.attempts + 1,
            successes=current.successes + (1 if success else 0),
        )
        candidates[skill_id] = updated
        self._rewrite(self._candidates_path(), candidates.values())
        return updated

    def promote(
        self,
        skill_id: str,
        *,
        approved_by: str,
        authored_names=(),
        min_successes: int = 2,
        min_attempts: int = 3,
        dedup_threshold: float = 0.6,
    ) -> SkillCandidate:
        """The HITL promotion gate (ADR 0005 §2.4).

        All four conditions must hold to enter the library: HITL approval,
        n-of-m reproducibility (``min_successes`` of ``min_attempts``), and no
        semantic duplication of an authored skill.
        """
        candidates = self._latest_candidates()
        if skill_id not in candidates:
            raise SkillPromotionError(f"unknown skill_id: {skill_id!r}")
        candidate = candidates[skill_id]
        if not approved_by or not str(approved_by).strip():
            raise SkillPromotionError("promotion requires a non-empty approved_by (HITL approval)")
        if candidate.successes < min_successes:
            raise SkillPromotionError(
                f"reproducibility not met: {candidate.successes} successes < {min_successes}"
            )
        if candidate.attempts < min_attempts:
            raise SkillPromotionError(
                f"reproducibility not met: {candidate.attempts} attempts < {min_attempts}"
            )
        self._reject_if_duplicates_authored(candidate, authored_names, dedup_threshold)
        self._append(self._promoted_path(), candidate)
        return candidate

    @staticmethod
    def _reject_if_duplicates_authored(
        candidate: SkillCandidate,
        authored_names: Sequence[str],
        dedup_threshold: float,
    ) -> None:
        """Reject promotion if the candidate duplicates an authored skill.

        Beyond the exact (case-insensitive) objective/skill_id match, a token
        Jaccard overlap >= ``dedup_threshold`` rejects a *near* duplicate, so a
        reworded authored playbook ("ship login fix" vs "Ship the login fix") is
        not re-earned -- the prior exact-string check missed those.
        """
        objective_norm = candidate.objective.strip().lower()
        objective_tokens = _tokens(candidate.objective)
        for name in authored_names:
            name_norm = str(name).strip().lower()
            if not name_norm:
                continue
            if objective_norm == name_norm or candidate.skill_id.lower() == name_norm:
                raise SkillPromotionError(
                    f"candidate duplicates an authored skill: {candidate.objective!r}"
                )
            if _jaccard(objective_tokens, _tokens(str(name))) >= dedup_threshold:
                raise SkillPromotionError(
                    f"candidate near-duplicates an authored skill: "
                    f"{candidate.objective!r} ~ {name!r}"
                )

    def candidates(self) -> list[SkillCandidate]:
        return list(self._latest_candidates().values())

    def promoted(self) -> list[SkillCandidate]:
        """Promoted skills, the authoritative latest row per ``skill_id``.

        ``promote`` is append-only, so re-promoting a skill appends another row;
        collapse to the latest like :meth:`candidates` so a consumer (e.g.
        :meth:`recall`) never sees or counts the same earned skill twice (codex
        #21)."""
        return list(self._latest_by_id(self._promoted_path()).values())

    def recall(
        self,
        objective: str,
        *,
        authored: Sequence[SkillCandidate] = (),
        limit: int = 3,
    ) -> list[SkillCandidate]:
        """Recall skills worth reusing for ``objective``, authored before earned.

        Ranks both the caller-supplied ``authored`` skills (gstack/Superpowers
        playbooks) and the promoted **earned** skills by how many content tokens
        their objective shares with ``objective``; skills that share none are
        dropped. Authored skills always sort ahead of earned ones (an authored
        playbook is the trusted source; an earned skill is a distilled guess),
        and earned skills tie-break on reproducibility (successes/attempts) so a
        more-proven procedure wins. Returns at most ``limit`` skills.

        This is the read side of the back-half loop: ``distill`` writes earned
        candidates and ``promote`` gates them in; ``recall`` feeds the promoted
        ones back into a run (``CausalityEngine.run_task`` attaches the result to
        the TaskRun and the ExecutionAdapter) so they are actually reused.
        """
        query = _tokens(objective)
        if not query:
            return []

        def overlap(skill: SkillCandidate) -> int:
            return len(query & _tokens(skill.objective))

        def reproducibility(skill: SkillCandidate) -> float:
            return skill.successes / skill.attempts if skill.attempts else 0.0

        authored_hits = sorted(
            (s for s in authored if overlap(s) > 0),
            key=lambda s: -overlap(s),
        )
        earned_hits = sorted(
            (s for s in self.promoted() if overlap(s) > 0),
            key=lambda s: (-overlap(s), -reproducibility(s)),
        )
        return (authored_hits + earned_hits)[:limit]

    def _latest_candidates(self) -> dict[str, SkillCandidate]:
        return self._latest_by_id(self._candidates_path())

    def _latest_by_id(self, path: Path) -> dict[str, SkillCandidate]:
        """Latest row per ``skill_id`` from an append-only JSONL skill file."""
        latest: dict[str, SkillCandidate] = {}
        for candidate in self._read(path):
            latest[candidate.skill_id] = candidate
        return latest

    def _read(self, path: Path) -> list[SkillCandidate]:
        return [
            SkillCandidate.from_dict(json.loads(line))
            for line in DurableJsonl(path).read_lines()
        ]

    def _append(self, path: Path, candidate: SkillCandidate) -> None:
        DurableJsonl(path).append(json.dumps(candidate.to_dict(), ensure_ascii=True))

    def _rewrite(self, path: Path, candidates: Any) -> None:
        DurableJsonl(path).rewrite(
            json.dumps(candidate.to_dict(), ensure_ascii=True) for candidate in candidates
        )
