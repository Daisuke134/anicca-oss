from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "agent_list_response.py"


def load_module():
    spec = importlib.util.spec_from_file_location("agent_list_response", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "payload",
    [
        {"agents": {"list": [{"agentId": "1", "agentStatus": "draft"}]}},
        {"agents": [{"agent_id": "1", "agent_status": "draft"}]},
        [{"agentId": "1", "agentStatus": "draft"}],
    ],
)
def test_parse_agent_list_accepts_supported_capafy_shapes(payload) -> None:
    module = load_module()

    assert module.parse_agent_list(payload) == [{"agentId": "1", "agentStatus": "draft"}]


@pytest.mark.parametrize("payload", [{}, {"agents": {}}, {"agents": ["bad"]}, "bad"])
def test_parse_agent_list_fails_closed_for_invalid_shapes(payload) -> None:
    module = load_module()

    with pytest.raises(ValueError):
        module.parse_agent_list(payload)
