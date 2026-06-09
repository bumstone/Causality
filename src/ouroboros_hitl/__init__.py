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
from .contract_harness import ContractHarness, ContractHarnessError
from .browser_adapter import A11yBrowserAdapter

__all__ = [
    "ActionType",
    "A11yBrowserAdapter",
    "AuditEventType",
    "ContractHarness",
    "ContractHarnessError",
    "EvidenceKind",
    "EvidenceLedger",
    "EvidenceRequirement",
    "GateDecision",
    "GateResult",
    "GoalContract",
    "HITLGate",
    "OuroborosHITL",
    "PermissionContract",
    "Risk",
    "StateTransition",
    "TaskContract",
    "VerifierDecision",
]
