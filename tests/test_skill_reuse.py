from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.contracts import EvidenceKind, VerifierDecision
from causality.durable import DurableJsonl
from causality.engine import CausalityEngine
from causality.skills import SkillCandidate, SkillStore


def _passing_verifiers():
    return [
        lambda c: VerifierDecision("correctness", "pass", "ok"),
        lambda c: VerifierDecision("evidence", "pass", "ok"),
    ]


def _evidence_work(engine: CausalityEngine):
    def work(contract, iteration, _adapter):
        engine.runtime.record_evidence(contract, EvidenceKind.TEST_OUTPUT, {"output": "ok"})

    return work


def _promote(store: SkillStore, candidate: SkillCandidate) -> None:
    DurableJsonl(store._promoted_path()).append(json.dumps(candidate.to_dict(), ensure_ascii=True))


class RecallRankingTests(unittest.TestCase):
    def _store(self, temp_dir: str) -> SkillStore:
        return SkillStore(Path(temp_dir))

    def test_recalls_only_objective_relevant_earned_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(temp_dir)
            _promote(store, SkillCandidate("s1", "parse recursive grammar productions", ()))
            _promote(store, SkillCandidate("s2", "deploy the release pipeline", ()))
            recalled = store.recall("parse the grammar")
            self.assertEqual([s.skill_id for s in recalled], ["s1"])

    def test_authored_ranks_before_earned_even_with_lower_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(temp_dir)
            _promote(store, SkillCandidate("earned", "parse recursive grammar productions", ()))
            authored = [SkillCandidate("authored", "grammar tips", ())]
            recalled = store.recall("parse the grammar", authored=authored)
            self.assertEqual([s.skill_id for s in recalled], ["authored", "earned"])

    def test_earned_tiebreak_on_reproducibility(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(temp_dir)
            _promote(store, SkillCandidate("low", "parse grammar", (), successes=1, attempts=4))
            _promote(store, SkillCandidate("high", "parse grammar", (), successes=3, attempts=4))
            recalled = store.recall("parse grammar")
            self.assertEqual([s.skill_id for s in recalled], ["high", "low"])

    def test_limit_is_respected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(temp_dir)
            for i in range(5):
                _promote(store, SkillCandidate(f"s{i}", "parse grammar variant", ()))
            self.assertEqual(len(store.recall("parse grammar", limit=2)), 2)

    def test_duplicate_promoted_rows_collapse_to_latest(self) -> None:
        # codex #21: promote is append-only, so a re-promotion leaves two rows
        # for one skill_id; recall must return it once (latest wins), not twice.
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(temp_dir)
            _promote(store, SkillCandidate("dup", "parse grammar", (), successes=1, attempts=2))
            _promote(store, SkillCandidate("dup", "parse grammar", (), successes=3, attempts=3))
            _promote(store, SkillCandidate("other", "parse grammar tokens", ()))
            recalled = store.recall("parse grammar", limit=5)
            ids = [s.skill_id for s in recalled]
            self.assertEqual(ids.count("dup"), 1)
            self.assertIn("other", ids)
            self.assertEqual(next(s for s in recalled if s.skill_id == "dup").successes, 3)

    def test_objective_with_no_content_tokens_recalls_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self._store(temp_dir)
            _promote(store, SkillCandidate("s1", "parse grammar", ()))
            self.assertEqual(store.recall(""), [])
            self.assertEqual(store.recall("a to of the"), [])


class EngineRecallTests(unittest.TestCase):
    def test_run_task_surfaces_recalled_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            _promote(engine.skills, SkillCandidate("s1", "parse recursive grammar", ()))
            run = engine.run_task(
                objective="parse the grammar",
                work=_evidence_work(engine),
                verifiers=_passing_verifiers(),
                verification=["pytest"],
                stop_condition={"max_iterations": 3},
            )
            self.assertIn("s1", [s.skill_id for s in run.recalled_skills])

    def test_gated_work_sees_recalled_skills_on_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            _promote(engine.skills, SkillCandidate("s1", "parse recursive grammar", ()))
            seen: dict[str, list[str]] = {}

            def work(contract, iteration, adapter):
                seen["ids"] = [s.skill_id for s in adapter.recalled_skills]
                adapter.execute(
                    tool="Bash",
                    action_kind="click",
                    description="run the unit tests",
                    run=lambda: engine.runtime.record_evidence(
                        contract, EvidenceKind.TEST_OUTPUT, {"output": "ok"}
                    ),
                )

            run = engine.run_task(
                objective="parse the grammar",
                work=work,
                verifiers=_passing_verifiers(),
                verification=["pytest"],
                stop_condition={"max_iterations": 3},
                allowed_tools=["Bash"],
            )
            self.assertEqual(seen["ids"], ["s1"])
            self.assertTrue(run.passed)

    def test_authored_skills_are_prioritized_in_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            _promote(engine.skills, SkillCandidate("earned", "parse recursive grammar productions", ()))
            authored = [SkillCandidate("authored", "grammar overview", ())]
            run = engine.run_task(
                objective="parse the grammar",
                work=_evidence_work(engine),
                verifiers=_passing_verifiers(),
                verification=["pytest"],
                stop_condition={"max_iterations": 3},
                authored_skills=authored,
            )
            ids = [s.skill_id for s in run.recalled_skills]
            self.assertEqual(ids[0], "authored")
            self.assertIn("earned", ids)

    def test_no_promoted_skills_means_empty_recall(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = CausalityEngine(Path(temp_dir))
            run = engine.run_task(
                objective="parse the grammar",
                work=_evidence_work(engine),
                verifiers=_passing_verifiers(),
                verification=["pytest"],
                stop_condition={"max_iterations": 3},
            )
            self.assertEqual(run.recalled_skills, ())


if __name__ == "__main__":
    unittest.main()
