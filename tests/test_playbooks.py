from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.agent_harness import _ROUTING
from causality.playbooks import (
    PLAYBOOKS,
    PlaybookPhase,
    UnknownPlaybookError,
    build_phase_plan,
    resolve_playbooks,
)


class PlaybookTests(unittest.TestCase):
    def test_resolve_returns_structured_phases(self) -> None:
        playbooks = resolve_playbooks(("tdd", "debugging"))
        self.assertEqual([p.name for p in playbooks], ["tdd", "debugging"])
        tdd = playbooks[0]
        self.assertEqual(tdd.phase_names, ("red", "green", "refactor"))
        self.assertTrue(all(isinstance(ph, PlaybookPhase) and ph.steps for ph in tdd.phases))

    def test_unknown_label_raises(self) -> None:
        with self.assertRaises(UnknownPlaybookError):
            resolve_playbooks(("not-a-real-playbook",))

    def test_empty_labels_resolve_to_empty(self) -> None:
        self.assertEqual(resolve_playbooks(()), ())  # the TRIVIAL bundle

    def test_every_routing_label_is_vendored(self) -> None:
        # The core closure: no routed bundle label is a dangling string -- every
        # one resolves to a structured, recorded playbook.
        for _architecture, labels in _ROUTING.values():
            with self.subTest(labels=labels):
                self.assertEqual(len(resolve_playbooks(labels)), len(labels))

    def test_playbook_to_dict_round_trips_structure(self) -> None:
        data = PLAYBOOKS["tdd"].to_dict()
        self.assertEqual(data["name"], "tdd")
        self.assertEqual([phase["name"] for phase in data["phases"]], ["red", "green", "refactor"])
        self.assertTrue(all(phase["steps"] for phase in data["phases"]))

    def test_root_cause_protocol_has_exact_order(self) -> None:
        playbook = resolve_playbooks(("root-cause-protocol",))[0]

        self.assertEqual(
            playbook.phase_names,
            ("reproduce", "hypothesis", "verify", "fix"),
        )

    def test_phase_plan_has_stable_ids_and_explicit_requirements(self) -> None:
        plan = build_phase_plan(resolve_playbooks(("root-cause-protocol",)))

        self.assertEqual(
            [item["phase_id"] for item in plan],
            [
                "root-cause-protocol/reproduce",
                "root-cause-protocol/hypothesis",
                "root-cause-protocol/verify",
                "root-cause-protocol/fix",
            ],
        )
        self.assertTrue(plan[0]["requires_action"])
        self.assertTrue(plan[2]["requires_verification"])
        self.assertEqual(plan[3]["requires_verdicts"], 2)

        # Public playbook serialization remains backward compatible.
        self.assertEqual(set(PLAYBOOKS["root-cause-protocol"].to_dict()), {"name", "summary", "phases"})
        self.assertEqual(
            set(PLAYBOOKS["root-cause-protocol"].to_dict()["phases"][0]),
            {"name", "steps"},
        )


if __name__ == "__main__":
    unittest.main()
