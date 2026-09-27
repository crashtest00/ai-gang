"""
Catalog-backed agent-assignment validation — a Python reimplementation of
services/scrummaster/src/assignment.js's exact acceptance rules, reused
since assignment must be validated against the same catalog-backed rules
platform-wide. See registry.py's module comment for why this is a
reimplementation reading the same on-disk catalog rather than a runtime
call into ScrumMaster, and the tradeoff that choice carries.

Pure and transport-agnostic, same as the original — no Jira/Redis/HTTP
calls, so callers on any transport can use it identically.
"""

from __future__ import annotations

from typing import Any

from . import registry

UNKNOWN_AGENT = 'UNKNOWN_AGENT'              # not a registered catalog id
AGENT_NOT_AVAILABLE = 'AGENT_NOT_AVAILABLE'  # registered, but not enabled for this project
UNKNOWN_PROJECT = 'UNKNOWN_PROJECT'          # projectName has no project configuration at all


def validate_assignment(project_name: str, agent_id: str) -> dict[str, Any]:
    """Validate a single proposed assignment. Returns {'ok': True, 'agent':
    ...} or {'ok': False, 'code', 'requestedAgent', 'permittedAgents'}."""
    project = registry.get_project(project_name)
    if not project:
        return {'ok': False, 'code': UNKNOWN_PROJECT, 'requestedAgent': agent_id, 'permittedAgents': []}

    permitted_agents = list(project['agents'])

    catalog_agent = registry.get_agent(agent_id)
    if not catalog_agent:
        return {'ok': False, 'code': UNKNOWN_AGENT, 'requestedAgent': agent_id, 'permittedAgents': permitted_agents}

    if agent_id not in permitted_agents:
        return {'ok': False, 'code': AGENT_NOT_AVAILABLE, 'requestedAgent': agent_id, 'permittedAgents': permitted_agents}

    return {'ok': True, 'agent': catalog_agent}


def validate_decomposition(project_name: str, subtasks: list[dict]) -> dict[str, Any]:
    """Validate an entire proposed decomposition atomically: every
    subtask's `agent` must be valid, or the whole batch is rejected
    together."""
    project = registry.get_project(project_name)
    permitted_agents = list(project['agents']) if project else []

    rejected = []
    for subtask in subtasks or []:
        result = validate_assignment(project_name, subtask.get('agent'))
        if not result['ok']:
            rejected.append({
                'subtaskId': subtask.get('id'),
                'displayName': subtask.get('displayName'),
                'requestedAgent': subtask.get('agent'),
            })

    if rejected:
        return {'ok': False, 'errorCode': 'INVALID_AGENT_ASSIGNMENT', 'rejected': rejected, 'permittedAgents': permitted_agents}
    return {'ok': True}
