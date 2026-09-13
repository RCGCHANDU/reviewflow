"""Output guardrail at the workflow boundary: a model may not claim an unearned status.

This is the guardrail for the failure mode "final response claims success when
verification failed". It is deliberately *not* a prompt instruction: the engine
computes the authoritative status from its gates, and this guardrail compares the
model's claim against it and trips the tripwire on any inflation.

Fail-closed: if no computed status is available, the claimed status cannot be
substantiated, so any claim at or above ``degraded`` trips.
"""

from __future__ import annotations

from agents import Agent, GuardrailFunctionOutput, RunContextWrapper
from agents.decorators import output_guardrail

from reviewflow.context import WorkflowContext
from reviewflow.models.contracts import FinalReport
from reviewflow.models.enums import WorkflowStatus

#: Higher = a stronger claim. Claiming above the computed level is inflation.
_STATUS_STRENGTH: dict[WorkflowStatus, int] = {
    WorkflowStatus.FAILED: 0,
    WorkflowStatus.BLOCKED: 1,
    WorkflowStatus.DEGRADED: 2,
    WorkflowStatus.SUCCEEDED: 3,
}


def status_inflation(claimed: WorkflowStatus, computed: WorkflowStatus | None) -> str | None:
    """Return a reason string when the claim exceeds what the gates earned."""
    if computed is None:
        if _STATUS_STRENGTH[claimed] >= _STATUS_STRENGTH[WorkflowStatus.DEGRADED]:
            return "no verification gate has run, so no success or degraded claim can be substantiated"
        return None
    if _STATUS_STRENGTH[claimed] > _STATUS_STRENGTH[computed]:
        return f"model claimed {claimed.value!r} but the workflow gates computed {computed.value!r}"
    return None


@output_guardrail
async def final_status_guardrail(
    ctx: RunContextWrapper[WorkflowContext],
    agent: Agent[WorkflowContext],
    output: FinalReport,
) -> GuardrailFunctionOutput:
    """Reject a final report whose status claim is stronger than the verified status."""
    context = ctx.context
    reason = status_inflation(output.claimed_status, context.computed_status)
    context.audit(
        "guardrail",
        guardrail="final_status",
        claimed=output.claimed_status.value,
        computed=context.computed_status.value if context.computed_status else None,
        triggered=reason is not None,
        reason=reason,
    )
    return GuardrailFunctionOutput(
        output_info={
            "claimed_status": output.claimed_status.value,
            "computed_status": context.computed_status.value if context.computed_status else None,
            "reason": reason,
        },
        tripwire_triggered=reason is not None,
    )
