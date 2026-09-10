"""Fail-closed normalization for Capafy's publish-list response."""
from __future__ import annotations

from typing import Any


_FIELD_ALIASES = {
    "agentId": ("agentId", "agent_id"),
    "name": ("name",),
    "description": ("description", "desc"),
    "agentType": ("agentType", "agent_type"),
    "agentStatus": ("agentStatus", "agent_status"),
    "latestAgentVersionId": ("latestAgentVersionId", "latest_agent_version_id"),
    "latestVersionName": ("latestVersionName", "latest_version_name"),
    "updatedAt": ("updatedAt", "updated_at"),
    "sales": ("sales",),
    "recentSales": ("recentSales", "recent_sales"),
}


def parse_agent_list(payload: Any) -> list[dict[str, Any]]:
    """Accept Capafy's known envelopes while rejecting unknown responses."""
    if isinstance(payload, list):
        raw_agents = payload
    elif isinstance(payload, dict):
        agents = payload.get("agents")
        raw_agents = agents.get("list") if isinstance(agents, dict) else agents
    else:
        raise ValueError("publish-list response is not an object or array")
    if not isinstance(raw_agents, list):
        raise ValueError("publish-list response has no agents list")

    normalized = []
    for index, agent in enumerate(raw_agents):
        if not isinstance(agent, dict):
            raise ValueError(f"publish-list agents[{index}] is not an object")
        row = {}
        for target, aliases in _FIELD_ALIASES.items():
            for source in aliases:
                if source in agent:
                    row[target] = agent[source]
                    break
        normalized.append(row)
    return normalized
