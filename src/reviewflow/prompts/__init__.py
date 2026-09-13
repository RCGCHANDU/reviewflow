"""Prompt composition and the role prompt catalogue."""

from reviewflow.prompts.library import (
    EVIDENCE_DISCIPLINE,
    UNTRUSTED_DATA_POLICY,
    RolePromptSpec,
    compose_role_prompt,
    corrective_feedback,
    delivery_instructions,
    render_output_contract,
    untrusted_material_block,
)
from reviewflow.prompts.roles import ROLE_SPECS

__all__ = [
    "EVIDENCE_DISCIPLINE",
    "ROLE_SPECS",
    "UNTRUSTED_DATA_POLICY",
    "RolePromptSpec",
    "compose_role_prompt",
    "corrective_feedback",
    "delivery_instructions",
    "render_output_contract",
    "untrusted_material_block",
]
