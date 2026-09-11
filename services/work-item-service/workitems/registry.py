"""
Catalog-backed agent/project registry — a Python reimplementation of the
parts of services/scrummaster/src/registry.js this service needs for catalog-backed
assignment validation (agent-assignment.md REQ-01/REQ-02, reused here per
canonical-work-model.md REQ-03/REQ-13's "validated against the same
catalog-backed validator agent-assignment.md already requires").

Architectural decision (see final report): a Python/Django service cannot
require() services/scrummaster/src/registry.js. Two options existed: (a) reimplement
the catalog-loading and validation logic in Python, reading the SAME
on-disk agents.json/projects.json files ScrumMaster's own registry.js
reads (AGENTS_CATALOG_PATH / PROJECTS_CONFIG_PATH — the exact same env var
names, so both services can be pointed at the same catalog files in any
deployment), or (b) call ScrumMaster's HTTP surface at runtime for every
single mutating command. (a) was chosen: the catalog is a small, rarely-
changing, already-shared-on-disk JSON configuration — not a live service
API — so reading it directly avoids adding a synchronous runtime
dependency (and a new failure mode: "work-item-service can't validate an
assignment because ScrumMaster's HTTP endpoint is down") to every write
this service accepts. The tradeoff this carries: two independent
implementations of the SAME validation rules must be kept in sync by hand
if either one changes — flagged explicitly in the final report as the
cost of this choice, not hidden.

Deliberately narrower than registry.js: this service has no A2A AgentCard
concept and no agent-catalog-authoring workflow of its own, so only the
catalog/project lookups assignment.py actually needs are ported
(getAgent, getProject, getProjectNames, normalizeProjectName). Validation
strictness (every catalog/project field required, duplicate-id detection)
is preserved because a malformed catalog should fail this service's
startup exactly as loudly as it fails ScrumMaster's.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Optional

from django.conf import settings

_lock = threading.Lock()
_catalog: Optional[dict[str, Any]] = None   # {'by_id': {...}, 'ids': [...], 'retired_ids': [...]}
_projects: Optional[dict[str, Any]] = None  # {name: {'name', 'jiraProjectKey', 'agents': [...]}}


def _catalog_path() -> str:
    return getattr(settings, 'AGENTS_CATALOG_PATH', None) or os.environ.get('AGENTS_CATALOG_PATH', '/app/config/agents.json')


def _projects_path() -> str:
    return getattr(settings, 'PROJECTS_CONFIG_PATH', None) or os.environ.get('PROJECTS_CONFIG_PATH', '/app/config/projects.json')


def _read_json(path: str) -> Any:
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def _parse_catalog(raw: dict, path: str) -> dict[str, Any]:
    errors = []
    agents = raw.get('agents')
    if not isinstance(agents, list):
        errors.append('"agents" must be a non-empty array')
        agents = []

    seen_ids = set()
    by_id = {}
    for i, entry in enumerate(agents):
        where = f'agents[{i}]'
        if not isinstance(entry, dict):
            errors.append(f'{where} must be an object')
            continue
        agent_id = entry.get('id')
        display_name = entry.get('displayName')
        definition_path = entry.get('definitionPath')
        routing = entry.get('routing') or {}

        if not agent_id or not isinstance(agent_id, str):
            errors.append(f'{where}.id is required and must be a non-empty string')
        elif agent_id in seen_ids:
            errors.append(f'duplicate agent id "{agent_id}" ({where})')
        else:
            seen_ids.add(agent_id)

        if not display_name or not isinstance(display_name, str):
            errors.append(f'{where}.displayName is required (id: {agent_id or "?"})')
        if not definition_path or not isinstance(definition_path, str):
            errors.append(f'{where}.definitionPath is required (id: {agent_id or "?"})')
        if not isinstance(routing, dict) or not routing.get('channelSuffix'):
            errors.append(f'{where}.routing.channelSuffix is required (id: {agent_id or "?"})')

        if agent_id and isinstance(agent_id, str) and display_name and definition_path and routing.get('channelSuffix'):
            by_id[agent_id] = entry

    retired_agents = raw.get('retiredAgents', [])
    if not isinstance(retired_agents, list):
        errors.append('"retiredAgents" must be an array if present')
        retired_agents = []
    for i, entry in enumerate(retired_agents):
        where = f'retiredAgents[{i}]'
        if not isinstance(entry, dict) or not entry.get('id'):
            errors.append(f'{where}.id is required')
            continue
        if entry['id'] in seen_ids:
            errors.append(f'"{entry["id"]}" appears in both agents and retiredAgents ({where})')

    if errors:
        joined = '\n  - '.join(errors)
        raise ValueError(f'Invalid agent catalog at {path}:\n  - {joined}')

    return {'by_id': by_id, 'ids': list(by_id.keys()), 'retired_ids': [r['id'] for r in retired_agents]}


def _parse_projects(raw: dict, path: str, catalog: dict[str, Any]) -> dict[str, Any]:
    errors = []
    projects_list = raw.get('projects')
    if not isinstance(projects_list, list):
        errors.append('"projects" must be a non-empty array')
        projects_list = []

    by_name = {}
    for i, entry in enumerate(projects_list):
        where = f'projects[{i}]'
        name = entry.get('name') if isinstance(entry, dict) else None
        if not name or not isinstance(name, str):
            errors.append(f'{where}.name is required')
            continue
        if name in by_name:
            errors.append(f'duplicate project name "{name}" ({where})')
        agent_ids = entry.get('agents')
        if not isinstance(agent_ids, list):
            errors.append(f'{where}.agents must be an array (project: {name})')
            agent_ids = []
        else:
            for aid in agent_ids:
                if aid not in catalog['by_id']:
                    errors.append(f'project "{name}" references unknown agent id "{aid}" — not present in agents.json')
        by_name[name] = {'name': name, 'jiraProjectKey': entry.get('jiraProjectKey'), 'agents': agent_ids}

    if errors:
        joined = '\n  - '.join(errors)
        raise ValueError(f'Invalid project configuration at {path}:\n  - {joined}')

    return by_name


def load(force: bool = False) -> None:
    global _catalog, _projects
    with _lock:
        if _catalog is not None and _projects is not None and not force:
            return
        c_path = _catalog_path()
        p_path = _projects_path()
        _catalog = _parse_catalog(_read_json(c_path), c_path)
        _projects = _parse_projects(_read_json(p_path), p_path, _catalog)


def get_agent(agent_id: str) -> Optional[dict[str, Any]]:
    load()
    return _catalog['by_id'].get(agent_id)


def get_project(name: str) -> Optional[dict[str, Any]]:
    load()
    return _projects.get(name)


def get_project_names() -> list[str]:
    load()
    return list(_projects.keys())


def get_all_agent_ids() -> list[str]:
    load()
    return list(_catalog['ids'])


def normalize_project_name(name: str) -> str:
    return str(name).strip().lower()


# Stream topology names ScrumMaster's own gateway/webhook flows use
# (redis-streams.md §4) — reused verbatim so webhook_consumer.py can read
# the SAME webhookStreamName ScrumMaster's server.js already publishes to
# (see that module's comment for why: a second, independent consumer group
# on ScrumMaster's existing stream, not a new stream).
WEBHOOK_GROUP = 'scrummaster'


def webhook_stream_name(project_name: str) -> str:
    return f'aigang:webhooks:{normalize_project_name(project_name)}'
