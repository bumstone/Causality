"""Causality integration primitives."""

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
from .orchestrator import Causality
from .contract_harness import BoundContract, ContractHarness, ContractHarnessError
from .loop import LoopResult, StepOutcome, run_bounded_loop
from .memory import MEMORY_TYPES, MemoryEntry, MemoryGovernanceError, TypedMemory
from .agent_harness import AgentHarness, Dispatch, TaskType
from .reflect import Reflection, reflect_on_contract
from .browser_adapter import A11yBrowserAdapter

__all__ = [
    "ActionType",
    "A11yBrowserAdapter",
    "AgentHarness",
    "AuditEventType",
    "BoundContract",
    "ContractHarness",
    "ContractHarnessError",
    "Dispatch",
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
    "Causality",
    "PermissionContract",
    "Reflection",
    "Risk",
    "StateTransition",
    "StepOutcome",
    "TaskContract",
    "TaskType",
    "TypedMemory",
    "VerifierDecision",
    "reflect_on_contract",
    "run_bounded_loop",
]
