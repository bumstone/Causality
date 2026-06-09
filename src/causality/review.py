"""Review step: call independent verifiers and aggregate their decisions
(ADR 0006 §6.1-2, the "Review" of Run -> Review -> Fix).

The orchestrator facade can *record* a :class:`VerifierDecision` and the
``HITLGate.complete`` gate can *judge* whether enough passed, but nothing
*calls* the reviewers -- ADR 0006 §6.1 marks Review 🟡 because verifier
decisions were injected ad hoc from outside. This module is that missing
caller: it runs a sequence of independent verifiers against a contract,
records each decision in the ledger (so ``complete`` sees them), and reports
the aggregate against the "two independent verifier passes" rule the
completion gate expects.

It only invokes and records; it does not transition state or call the gate.
Standardizing the caller -- rather than letting each run inject verifier
decisions by hand -- keeps the ledger the single source the completion gate
reads (ADR 0006 §6.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Sequence

from .contracts import GoalContract, VerifierDecision

if TYPE_CHECKING:
    from .orchestrator import Causality


Verifier = Callable[[GoalContract], VerifierDecision]


@dataclass(frozen=True)
class ReviewResult:
    """The aggregate of one Review pass over a contract.

    ``approved`` encodes the completion gate's expectation: at least
    ``min_passes`` independent verifier passes *and* no critical failure.
    """

    decisions: tuple[VerifierDecision, ...]
    passes: int
    has_critical_failure: bool
    approved: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "decisions": [decision.to_dict() for decision in self.decisions],
            "passes": self.passes,
            "has_critical_failure": self.has_critical_failure,
            "approved": self.approved,
        }


def run_review(
    runtime: "Causality",
    contract: GoalContract,
    verifiers: Sequence[Verifier],
    *,
    min_passes: int = 2,
) -> ReviewResult:
    """Run each verifier against ``contract`` and aggregate the verdict.

    Each verifier is called with the contract to produce a
    :class:`VerifierDecision`, which is recorded via
    ``runtime.record_verifier`` so it lands in the ledger the completion gate
    reads. The decisions are returned in verifier order.

    Aggregation:

    - ``passes`` is the count of decisions where ``is_pass``.
    - ``has_critical_failure`` is true if any decision ``is_critical_failure``.
    - ``approved`` is ``passes >= min_passes`` and not ``has_critical_failure``
      -- the "two independent verifier passes" rule (ADR 0006 §6.2).
    """
    decisions: list[VerifierDecision] = []
    for verifier in verifiers:
        decision = verifier(contract)
        runtime.record_verifier(contract, decision)
        decisions.append(decision)

    passes = sum(1 for decision in decisions if decision.is_pass)
    has_critical_failure = any(decision.is_critical_failure for decision in decisions)
    approved = passes >= min_passes and not has_critical_failure

    return ReviewResult(
        decisions=tuple(decisions),
        passes=passes,
        has_critical_failure=has_critical_failure,
        approved=approved,
    )
