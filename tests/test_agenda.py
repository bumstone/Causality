from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from causality.agenda import VALID_STATUSES, Agenda, AgendaError, AgendaItem


class AgendaTests(unittest.TestCase):
    def test_add_then_items_returns_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            agenda = Agenda(Path(temp_dir) / "agenda.json")
            item = agenda.add("ship the agenda store")

            self.assertIsInstance(item, AgendaItem)
            self.assertEqual(item.status, "pending")
            self.assertIn(item.status, VALID_STATUSES)
            self.assertTrue(item.item_id)

            all_items = agenda.items()
            self.assertEqual(len(all_items), 1)
            self.assertEqual(all_items[0].objective, "ship the agenda store")

    def test_blank_objective_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            agenda = Agenda(Path(temp_dir) / "agenda.json")
            with self.assertRaises(AgendaError):
                agenda.add("   ")

    def test_persistence_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "agenda.json"
            first = Agenda(path)
            first.add("low", priority=1)
            first.add("high", priority=9)

            again = Agenda(path)
            objectives = {item.objective for item in again.items()}
            self.assertEqual(objectives, {"low", "high"})
            self.assertEqual(len(again.items()), 2)

    def test_priority_ordering_and_next_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            agenda = Agenda(Path(temp_dir) / "agenda.json")
            agenda.add("low", priority=1)
            top = agenda.add("high", priority=9)
            agenda.add("mid", priority=5)

            ordered = [item.objective for item in agenda.items()]
            self.assertEqual(ordered, ["high", "mid", "low"])

            nxt = agenda.next_pending()
            self.assertIsNotNone(nxt)
            assert nxt is not None
            self.assertEqual(nxt.item_id, top.item_id)

    def test_created_at_tiebreak_for_equal_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            agenda = Agenda(Path(temp_dir) / "agenda.json")
            first = agenda.add("first")
            agenda.add("second")
            # Equal priority: oldest created_at first (stable tiebreak).
            self.assertEqual(agenda.next_pending().item_id, first.item_id)

    def test_activated_item_no_longer_next_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            agenda = Agenda(Path(temp_dir) / "agenda.json")
            top = agenda.add("high", priority=9)
            second = agenda.add("mid", priority=5)

            agenda.activate(top.item_id)
            nxt = agenda.next_pending()
            self.assertIsNotNone(nxt)
            assert nxt is not None
            self.assertEqual(nxt.item_id, second.item_id)

    def test_dropped_item_no_longer_next_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            agenda = Agenda(Path(temp_dir) / "agenda.json")
            top = agenda.add("high", priority=9)
            second = agenda.add("mid", priority=5)

            agenda.drop(top.item_id)
            nxt = agenda.next_pending()
            self.assertIsNotNone(nxt)
            assert nxt is not None
            self.assertEqual(nxt.item_id, second.item_id)

    def test_transitions_update_status_and_persist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "agenda.json"
            agenda = Agenda(path)
            a = agenda.add("a")
            b = agenda.add("b")
            c = agenda.add("c")

            activated = agenda.activate(a.item_id)
            completed = agenda.complete(b.item_id)
            dropped = agenda.drop(c.item_id)

            self.assertEqual(activated.status, "active")
            self.assertEqual(completed.status, "done")
            self.assertEqual(dropped.status, "dropped")

            # Each status is filterable and persisted to a fresh store.
            reloaded = Agenda(path)
            self.assertEqual([i.item_id for i in reloaded.items(status="active")], [a.item_id])
            self.assertEqual([i.item_id for i in reloaded.items(status="done")], [b.item_id])
            self.assertEqual([i.item_id for i in reloaded.items(status="dropped")], [c.item_id])
            self.assertEqual(reloaded.items(status="pending"), [])

    def test_unknown_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            agenda = Agenda(Path(temp_dir) / "agenda.json")
            agenda.add("present")
            with self.assertRaises(AgendaError):
                agenda.activate("does-not-exist")
            with self.assertRaises(AgendaError):
                agenda.complete("does-not-exist")
            with self.assertRaises(AgendaError):
                agenda.drop("does-not-exist")


if __name__ == "__main__":
    unittest.main()
