"""Ouroboros HITL integration primitives."""

from .contracts import (
    ActionType,
    AuditEventType,
    EvidenceKind,
    EvidenceRequirement,
    GateDecision,
    GoalContract,
    PermissionContract,
    Risk,
    StateTransition,
    TaskContract,
    VerifierDecision,
)
from .gates import GateResult, HITLGate
from .ledger import EvidenceLedger
from .orchestrator import OuroborosHITL
from .contract_harness import BoundContract, ContractHarness, ContractHarnessError
from .loop import LoopResult, StepOutcome, run_bounded_loop
from .memory import MEMORY_TYPES, MemoryEntry, MemoryGovernanceError, TypedMemory
from .browser_adapter import A11yBrowserAdapter

__all__ = [
    "ActionType",
    "A11yBrowserAdapter",
    "AuditEventType",
    "BoundContract",
    "ContractHarness",
    "ContractHarnessError",
    "EvidenceKind",
    "EvidenceLedger",
    "EvidenceRequirement",
    "GateDecision",
    "GateResult",
    "GoalContract",
    "HITLGate",
    "LoopResult",
    "MEMORY_TYPES",
    "MemoryEntry",
    "MemoryGovernanceError",
    "OuroborosHITL",
    "PermissionContract",
    "Risk",
    "StateTransition",
    "StepOutcome",
    "TaskContract",
    "TypedMemory",
    "VerifierDecision",
    "run_bounded_loop",
]
