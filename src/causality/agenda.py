"""Agenda — long-term goal / pending-work backlog (ADR 0005 §2.3).

The agenda is a backlog of **pre-contract intentions** that sit *above* any
individual ``GoalContract`` (``contracts.py:135``). An item is not a goal
specification yet; it is a standing intention the Agent Harness (ADR 0004)
picks up and instantiates into a ``GoalContract`` when it chooses to act on it,
so the agenda never duplicates the contract layer.

Agenda *content* is runtime state (ADR 0008): it is persisted as a single JSON
state file under ``self.path`` (``{"items": [...]}``) which the maintainer
gitignores. The store loads on init and saves after every mutation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .contracts import utc_now
from .durable import write_text_durably

VALID_STATUSES = ("pending", "active", "done", "dropped")


class AgendaError(ValueError):
    """Raised when an agenda operation is invalid (blank objective, unknown id)."""


@dataclass(frozen=True)
class AgendaItem:
    item_id: str
    objective: str
    priority: int = 0
    status: str = "pending"
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "objective": self.objective,
            "priority": self.priority,
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "AgendaItem":
        return cls(
            item_id=value["item_id"],
            objective=value["objective"],
            priority=int(value.get("priority", 0)),
            status=value.get("status", "pending"),
            created_at=value.get("created_at", ""),
        )


@dataclass
class Agenda:
    """Backlog of pre-contract intentions persisted to a JSON state file."""

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._items: list[AgendaItem] = []
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._items = [AgendaItem.from_dict(item) for item in data.get("items", [])]

    def _save(self) -> None:
        payload = {"items": [item.to_dict() for item in self._items]}
        write_text_durably(
            self.path, json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
        )

    def add(self, objective: str, *, priority: int = 0) -> AgendaItem:
        objective = objective.strip()
        if not objective:
            raise AgendaError("objective is required")
        item = AgendaItem(
            item_id=uuid4().hex,
            objective=objective,
            priority=priority,
            status="pending",
        )
        self._items.append(item)
        self._save()
        return item

    def items(self, *, status: str | None = None) -> list[AgendaItem]:
        selected = self._items
        if status is not None:
            selected = [item for item in selected if item.status == status]
        # Higher priority first; oldest created_at first as a stable tiebreak.
        return sorted(selected, key=lambda item: (-item.priority, item.created_at))

    def next_pending(self) -> AgendaItem | None:
        pending = self.items(status="pending")
        return pending[0] if pending else None

    def _transition(self, item_id: str, status: str) -> AgendaItem:
        for index, item in enumerate(self._items):
            if item.item_id == item_id:
                updated = AgendaItem(
                    item_id=item.item_id,
                    objective=item.objective,
                    priority=item.priority,
                    status=status,
                    created_at=item.created_at,
                )
                self._items[index] = updated
                self._save()
                return updated
        raise AgendaError(f"unknown agenda item: {item_id!r}")

    def activate(self, item_id: str) -> AgendaItem:
        return self._transition(item_id, "active")

    def complete(self, item_id: str) -> AgendaItem:
        return self._transition(item_id, "done")

    def drop(self, item_id: str) -> AgendaItem:
        return self._transition(item_id, "dropped")

    def defer(self, item_id: str) -> AgendaItem:
        """Return an active item to the pending queue.

        Used when a run did not finish the item (failed, escalated, or raised),
        so the intention stays visible instead of being stranded "active"
        forever (code review 2026-06-13, F10).
        """
        return self._transition(item_id, "pending")
