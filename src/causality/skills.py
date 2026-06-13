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
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts import GoalContract
from .ledger import EvidenceLedger


@dataclass(frozen=True)
class SkillCandidate:
    skill_id: str
    objective: str
    steps: tuple[str, ...]
    provenance: str | None = None
    attempts: int = 0
    successes: int = 0

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "objective": self.objective,
            "steps": list(self.steps),
            "provenance": self.provenance,
            "attempts": self.attempts,
            "successes": self.successes,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "SkillCandidate":
        return cls(
            skill_id=value["skill_id"],
            objective=value["objective"],
            steps=tuple(value.get("steps", ())),
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
        steps = tuple(
            f"{event.event_type}:"
            f"{event.payload.get('kind') or event.payload.get('decision') or ''}"
            for event in matches
        )
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
        authored_lower = {str(name).strip().lower() for name in authored_names}
        if candidate.objective.strip().lower() in authored_lower or candidate.skill_id.lower() in authored_lower:
            raise SkillPromotionError(
                f"candidate duplicates an authored skill: {candidate.objective!r}"
            )
        self._append(self._promoted_path(), candidate)
        return candidate

    def candidates(self) -> list[SkillCandidate]:
        return list(self._latest_candidates().values())

    def promoted(self) -> list[SkillCandidate]:
        return self._read(self._promoted_path())

    def _latest_candidates(self) -> dict[str, SkillCandidate]:
        latest: dict[str, SkillCandidate] = {}
        for candidate in self._read(self._candidates_path()):
            latest[candidate.skill_id] = candidate
        return latest

    def _read(self, path: Path) -> list[SkillCandidate]:
        if not path.exists():
            return []
        records: list[SkillCandidate] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(SkillCandidate.from_dict(json.loads(line)))
        return records

    def _append(self, path: Path, candidate: SkillCandidate) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(candidate.to_dict(), ensure_ascii=True) + "\n")

    def _rewrite(self, path: Path, candidates: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(candidate.to_dict(), ensure_ascii=True) for candidate in candidates
        ]
        path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
