"""The agent's AgentHandlerProtocol declares every member the engine's declares.

guard-core's protocol declares the sync ``get_stats()``; this one declared only
the async ``get_status()``. Both exist on the handler and the engine calls both
(CompositeAgentHandler.get_status and .get_stats read them with getattr), so the
agent's protocol declares both and the engine's members are a subset of it.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from guard_core.protocols.agent_protocol import (
    AgentHandlerProtocol as EngineAgentHandlerProtocol,
)

from guard_agent.client import GuardAgentHandler
from guard_agent.models import AgentConfig
from guard_agent.protocols import AgentHandlerProtocol


def _members(protocol: type) -> set[str]:
    return {
        name
        for name, value in vars(protocol).items()
        if not name.startswith("_") and (callable(value) or isinstance(value, property))
    }


@pytest.fixture
def handler() -> Generator[GuardAgentHandler, None, None]:
    GuardAgentHandler._instance = None
    yield GuardAgentHandler(
        AgentConfig(
            api_key="test-api-key", endpoint="http://localhost:8000", project_id="p"
        )
    )
    GuardAgentHandler._instance = None


def test_agent_protocol_declares_every_engine_protocol_member() -> None:
    assert (
        _members(EngineAgentHandlerProtocol) - _members(AgentHandlerProtocol) == set()
    )


def test_agent_protocol_declares_get_stats_and_get_status() -> None:
    assert {"get_stats", "get_status"} <= _members(AgentHandlerProtocol)


def test_the_handler_satisfies_both_protocols(handler: GuardAgentHandler) -> None:
    assert isinstance(handler, AgentHandlerProtocol)
    assert isinstance(handler, EngineAgentHandlerProtocol)
