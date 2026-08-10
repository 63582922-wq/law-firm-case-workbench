"""Public, build-time manifest of the Agent's governed capability surface.

The desktop UI may describe what the Agent can request, but must not carry an
independent hand-maintained list that can drift from the policy registry.  This
module intentionally exports policy metadata only: no paths, providers,
credentials, case data, or executable handles.
"""

from __future__ import annotations

import json
from typing import Any

from .skill_registry import CaseSkillRegistry, default_case_skill_registry


SCHEMA_VERSION = "case-agent-capabilities-v1"


def build_case_agent_capability_manifest(
    registry: CaseSkillRegistry | None = None,
) -> dict[str, Any]:
    """Return only UI-safe capability and gate metadata in a stable order."""

    current = registry or default_case_skill_registry()
    skills = []
    for skill in current.list_skills():
        skills.append(
            {
                "skill_id": skill.skill_id,
                "version": skill.version,
                "title": skill.title,
                "maturity": skill.maturity.value,
                "approval_gate": skill.approval_gate.value,
                "required_scopes": sorted(scope.value for scope in skill.required_scopes),
                "allowed_tools": list(skill.allowed_tools),
                "output_kind": skill.output_kind,
                "prohibited_actions": list(skill.prohibited_actions),
            }
        )
    return {"schema_version": SCHEMA_VERSION, "skills": skills}


def render_case_agent_capability_manifest(
    registry: CaseSkillRegistry | None = None,
) -> str:
    return json.dumps(
        build_case_agent_capability_manifest(registry),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
