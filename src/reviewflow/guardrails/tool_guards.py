"""Defence-in-depth tool guardrails.

``read_only_tool_guard`` is attached to every read-only tool. It denies any call
to a tool the permission registry marks as mutating. That makes the read-only
property enforced at three independent levels:

1. reviewers are built without mutating tools (structural);
2. this SDK tool guardrail refuses a mutating call before execution;
3. the mutating tool itself demands an edit grant (authorisation).
"""

from __future__ import annotations

from agents.decorators import tool_input_guardrail
from agents.tool_guardrails import ToolGuardrailFunctionOutput, ToolInputGuardrailData

from reviewflow.context import WorkflowContext
from reviewflow.permissions import is_mutating


@tool_input_guardrail
def read_only_tool_guard(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Refuse mutating tools when the caller has no edit grant."""
    tool_context = data.context
    app_context = getattr(tool_context, "context", None)
    tool_name = getattr(tool_context, "tool_name", "") or ""
    mutating = is_mutating(tool_name)

    if mutating:
        if not isinstance(app_context, WorkflowContext):
            return ToolGuardrailFunctionOutput.reject_content(
                f"tool {tool_name} denied: mutation requires an application context"
            )
        if app_context.edit_grant is None:
            app_context.audit("guardrail_blocked", tool=tool_name, reason="no active edit grant")
            return ToolGuardrailFunctionOutput.reject_content(
                f"tool {tool_name} denied: no active edit grant for this workflow stage"
            )

    if not isinstance(app_context, WorkflowContext):
        return ToolGuardrailFunctionOutput.reject_content(
            f"tool {tool_name} denied: tool calls require a WorkflowContext"
        )
    return ToolGuardrailFunctionOutput.allow()
