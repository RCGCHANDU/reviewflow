"""SDK guardrails plus the application validators they do not replace.

SDK guardrail semantics (verified against openai-agents 0.22.2):

* **input** guardrails run only for the *first* agent of a chain;
* **output** guardrails run only for the agent that produces the *final* output;
* **tool** guardrails run on every guarded tool call, including intermediate ones.

So agent-level guardrails are used only at genuine workflow boundaries (task
entry, final report), while intermediate specialist output is validated by
:mod:`reviewflow.validation`, by tool guardrails, and by the state machine.
"""

from reviewflow.guardrails.final_status import final_status_guardrail
from reviewflow.guardrails.task_scope import task_scope_guardrail
from reviewflow.guardrails.tool_guards import read_only_tool_guard

__all__ = ["final_status_guardrail", "read_only_tool_guard", "task_scope_guardrail"]
