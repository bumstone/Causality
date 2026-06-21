"""L1 Dispatch -- Agent Harness task-type router (ADR 0004 / ADR 0006 §2 L1).

This is the single dispatch point that classifies a task by *type* and selects
exactly **one** playbook bundle from one upstream architecture. It never blends
architectures (ADR 0004 §2, alternative A rejected; ADR 0006 C6): each task type
maps to one bundle, and trivial work is answered directly with no playbook.

The harness decides *what* to run (which bundle). It does not decide *how* to
stage it -- that is the L3 Stage Designer's responsibility, and the selected
bundle owns its own phase ordering (ADR 0004 C-ROUTE-2). Routing here replaces
the legacy ``agent-rules`` intent routing (ADR 0006 C1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .playbooks import Playbook, resolve_playbooks


class TaskType(str, Enum):
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    LONG_RUNNING = "long_running"
    RELEASE = "release"
    TRIVIAL = "trivial"


# Routing table (ADR 0004 §2): one bundle per task type, never blended.
# Maps each task type to (upstream architecture, playbook bundle).
_ROUTING: dict[TaskType, tuple[str, tuple[str, ...]]] = {
    TaskType.PLANNING: ("gstack", ("office-hours", "ceo-review")),
    TaskType.IMPLEMENTATION: ("superpowers", ("tdd", "debugging")),
    TaskType.LONG_RUNNING: ("causality", ("contract-harness", "limited-causality-loop")),
    TaskType.RELEASE: ("gstack", ("ship", "qa-checklist")),
    TaskType.TRIVIAL: ("", ()),
}


# Deterministic, case-insensitive keyword heuristic for classify(). Order of
# precedence matches the ADR's if/elif chain: long-running is checked before
# implementation/release so that an "autonomous overnight refactor" routes to the
# bounded Causality loop rather than to plain implementation.
CLASSIFY_KEYWORDS: dict[TaskType, tuple[str, ...]] = {
    TaskType.LONG_RUNNING: (
        "long-running",
        "long running",
        "autonomous",
        "overnight",
        "unattended",
    ),
    TaskType.RELEASE: ("release", "ship", "deploy", "publish"),
    TaskType.PLANNING: ("plan", "design", "spec", "brainstorm", "idea"),
    TaskType.IMPLEMENTATION: (
        "implement",
        "feature",
        "fix",
        "bug",
        "refactor",
        "test",
    ),
}

# Precedence for tie-breaking when keywords from multiple types appear.
_CLASSIFY_ORDER: tuple[TaskType, ...] = (
    TaskType.LONG_RUNNING,
    TaskType.RELEASE,
    TaskType.PLANNING,
    TaskType.IMPLEMENTATION,
)


# High-confidence "this is risky" signals (destructive / financial / secret /
# infra / access-changing). When the task-type keywords miss but one of these is
# present, the request is NOT trivial: classifying it TRIVIAL would answer such
# work directly and bypass the Contract Harness and its gates. classify() fails
# safe to a governed type instead. Matching is intentionally liberal -- a false
# positive only over-governs a benign task (safe), while a miss reopens the
# bypass -- and stems use a leading boundary so inflections match.
_SENSITIVE_SIGNAL = re.compile(
    r"\b(?:"
    r"delet|remov|wipe|wiping|eras|destroy|truncat|"          # destructive (stemmed)
    r"payment|billing|invoic|charg|refund|payout|"            # financial
    r"credential|password|passwd|secret|token|api[\s_-]?key|"  # secrets (incl. "api key")
    r"deploy|migrat|database|production|rollback|"            # infra/prod
    r"permission|revok|grant|chmod|chown|sudo|overwrit"       # access/ops
    r")",
    re.IGNORECASE,
)

# Where sensitive-but-unclassified work is routed: a governed type (its bundle
# plus the bound contract's gates), never TRIVIAL.
_SENSITIVE_FALLBACK = TaskType.IMPLEMENTATION


@dataclass(frozen=True)
class Dispatch:
    task_type: TaskType
    architecture: str  # "gstack" | "superpowers" | "causality" | "" for trivial
    playbook: tuple[str, ...]  # the chosen bundle, e.g. ("office-hours", "ceo-review")

    def to_dict(self) -> dict[str, object]:
        return {
            "task_type": self.task_type.value,
            "architecture": self.architecture,
            "playbook": list(self.playbook),
        }


class AgentHarness:
    """L1 dispatcher: classify a task and route it to a single playbook bundle."""

    def route(self, task_type: TaskType | str) -> Dispatch:
        """Return the :class:`Dispatch` for ``task_type``.

        Accepts a :class:`TaskType` or its string value. Raises ``ValueError``
        on any unknown input.
        """
        try:
            resolved = task_type if isinstance(task_type, TaskType) else TaskType(task_type)
        except ValueError as exc:
            raise ValueError(f"unknown task type: {task_type!r}") from exc

        architecture, playbook = _ROUTING[resolved]
        return Dispatch(task_type=resolved, architecture=architecture, playbook=playbook)

    def playbooks(self, dispatch: Dispatch) -> tuple[Playbook, ...]:
        """Resolve a dispatch's bundle labels to the vendored playbooks.

        Every label in :data:`_ROUTING` resolves to a structured
        :class:`~causality.playbooks.Playbook`; an unknown label raises so the
        routing table can never point at a non-existent playbook. TRIVIAL routes
        to no bundle, so this returns ``()``.
        """
        return resolve_playbooks(dispatch.playbook)

    def classify(self, text: str, *, default: TaskType = TaskType.TRIVIAL) -> TaskType:
        """Map free text to a :class:`TaskType` via the keyword heuristic.

        Case-insensitive, matched on a *leading word boundary* against
        :data:`CLASSIFY_KEYWORDS` in :data:`_CLASSIFY_ORDER`. The leading
        boundary stops a keyword from matching inside an unrelated word (e.g.
        "test" must not match "latest" / "contest" / "protest"; codex review
        r3382219473) while still allowing inflections ("tests", "deploying",
        "planning").

        Fail-safe fallback (ADR 0004 §2): when nothing matches, text carrying a
        sensitive/irreversible signal (:data:`_SENSITIVE_SIGNAL` -- delete,
        deploy, payment, production, credentials, ...) is routed to a *governed*
        type rather than ``TRIVIAL``, so destructive or sensitive work cannot be
        answered directly and bypass the contract gates. Only genuinely
        keyword-free, non-sensitive text falls to ``default`` (``TRIVIAL``); pass
        ``default=`` to govern all unmatched text.
        """
        haystack = (text or "").lower()
        for task_type in _CLASSIFY_ORDER:
            for keyword in CLASSIFY_KEYWORDS[task_type]:
                if re.search(r"\b" + re.escape(keyword), haystack):
                    return task_type
        if _SENSITIVE_SIGNAL.search(haystack):
            return _SENSITIVE_FALLBACK
        return default
