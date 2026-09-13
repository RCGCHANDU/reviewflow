"""Audit ledger: persistence isolated behind an interface.

The workflow writes every stage, agent call, retry, transition, guardrail
failure, verification outcome and trace span here. Conversation history is never
the database: the ledger is.
"""

from reviewflow.persistence.ledger import Ledger, LedgerFactory, open_ledger
from reviewflow.persistence.records import AuditEvent, EventType, TraceSpanRecord, WorkflowRunRecord

__all__ = [
    "AuditEvent",
    "EventType",
    "Ledger",
    "LedgerFactory",
    "TraceSpanRecord",
    "WorkflowRunRecord",
    "open_ledger",
]
