"""Explicit workflow state machine.

Orchestration state lives here, not inside an LLM's reasoning. Every transition
is validated against a table, recorded in the audit ledger, and testable on its
own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from reviewflow.errors import IllegalTransition
from reviewflow.models.enums import Stage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from reviewflow.persistence.ledger import Ledger

#: The transition table. Anything absent is illegal, including self-transitions.
ALLOWED_TRANSITIONS: dict[Stage, frozenset[Stage]] = {
    Stage.INGEST: frozenset({Stage.PLAN, Stage.FAILED}),
    Stage.PLAN: frozenset({Stage.PARALLEL_REVIEW, Stage.FAILED}),
    Stage.PARALLEL_REVIEW: frozenset({Stage.SYNTHESIZE, Stage.FAILED}),
    # SYNTHESIZE goes straight to ADVERSARIAL_REVIEW when nothing needs editing: the
    # decision *not* to edit is itself adversarially tested before verification.
    Stage.SYNTHESIZE: frozenset({Stage.CONTROLLED_EDIT, Stage.ADVERSARIAL_REVIEW, Stage.FAILED}),
    Stage.CONTROLLED_EDIT: frozenset({Stage.DIFF_REVIEW, Stage.FAILED}),
    Stage.DIFF_REVIEW: frozenset({Stage.ADVERSARIAL_REVIEW, Stage.FAILED}),
    Stage.ADVERSARIAL_REVIEW: frozenset({Stage.VERIFY, Stage.FAILED}),
    Stage.VERIFY: frozenset({Stage.FINAL_REVIEW, Stage.REVISE, Stage.BLOCKED, Stage.FAILED}),
    Stage.REVISE: frozenset({Stage.SYNTHESIZE, Stage.BLOCKED, Stage.FAILED}),
    Stage.FINAL_REVIEW: frozenset({Stage.COMPLETE, Stage.FAILED}),
    Stage.COMPLETE: frozenset(),
    Stage.BLOCKED: frozenset(),
    Stage.FAILED: frozenset(),
}

#: Stages from which no further transition is possible.
TERMINAL_STAGES: frozenset[Stage] = frozenset({Stage.COMPLETE, Stage.BLOCKED, Stage.FAILED})


class StateMachine:
    """Tracks and validates workflow stage transitions."""

    def __init__(self, workflow_id: str, ledger: Ledger | None = None, *, start: Stage = Stage.INGEST) -> None:
        self.workflow_id = workflow_id
        self.ledger = ledger
        self.stage = start
        self.history: list[Stage] = [start]

    def can_transition(self, target: Stage) -> bool:
        return target in ALLOWED_TRANSITIONS[self.stage]

    def transition(self, target: Stage, *, reason: str = "") -> Stage:
        """Move to ``target``, recording the transition. Raises on an illegal move."""
        if not self.can_transition(target):
            raise IllegalTransition(
                f"{self.workflow_id}: illegal transition {self.stage.value} -> {target.value}"
            )
        previous, self.stage = self.stage, target
        self.history.append(target)
        if self.ledger is not None:
            from reviewflow.persistence.records import AuditEvent, EventType

            self.ledger.record(
                AuditEvent(
                    workflow_id=self.workflow_id,
                    event_type=EventType.TRANSITION,
                    stage=target,
                    transition_from=previous,
                    transition_to=target,
                    detail={"reason": reason},
                )
            )
        return target

    def is_terminal(self) -> bool:
        return self.stage in TERMINAL_STAGES

    def stages_completed(self) -> list[Stage]:
        """Unique stages entered, in order, excluding the terminal failure states."""
        seen: list[Stage] = []
        for stage in self.history:
            if stage not in seen:
                seen.append(stage)
        return [s for s in seen if s not in {Stage.FAILED, Stage.BLOCKED}]
