from collections.abc import Callable, Coroutine, Generator

import pytest

from guard_agent._json_types import JsonValue
from guard_agent.client import GuardAgentHandler
from guard_agent.models import AgentConfig
from guard_agent.protocols import TransportProtocol
from guard_agent.transport import HTTPTransport


@pytest.fixture(autouse=True)
def reset_singleton() -> Generator[None, None, None]:
    GuardAgentHandler._instance = None
    yield
    GuardAgentHandler._instance = None


async def _accepted(*args: object) -> bool:
    return True


async def _no_rules(*args: object) -> None:
    return None


def _closed_breaker_stats(self: object) -> dict[str, JsonValue]:
    return {"circuit_breaker_state": "CLOSED", "requests_sent": 0}


_IMPLEMENTATIONS: dict[str, Callable[..., Coroutine[None, None, object] | object]] = {
    "send_events": _accepted,
    "send_metrics": _accepted,
    "send_status": _accepted,
    "fetch_dynamic_rules": _no_rules,
    "get_stats": _closed_breaker_stats,
}


def _protocol_members() -> list[str]:
    return [
        name
        for name, member in vars(TransportProtocol).items()
        if callable(member) and not name.startswith("_")
    ]


def _transport_from_protocol_alone() -> object:
    namespace = {name: _IMPLEMENTATIONS[name] for name in _protocol_members()}
    return type("CustomTransport", (), namespace)()


def test_protocol_declares_get_stats() -> None:
    assert "get_stats" in _protocol_members()


def test_every_protocol_member_has_a_test_implementation() -> None:
    assert set(_protocol_members()) == set(_IMPLEMENTATIONS)


def test_agent_get_stats_serves_a_transport_built_from_the_protocol_alone(
    agent_config: AgentConfig,
) -> None:
    transport = _transport_from_protocol_alone()
    assert isinstance(transport, TransportProtocol)
    agent = GuardAgentHandler(agent_config)
    agent.__dict__["transport"] = transport

    stats = agent.get_stats()

    assert stats["transport_stats"] == {
        "circuit_breaker_state": "CLOSED",
        "requests_sent": 0,
    }


def test_a_transport_without_get_stats_does_not_satisfy_the_protocol() -> None:
    members = [name for name in _protocol_members() if name != "get_stats"]
    incomplete = type(
        "IncompleteTransport", (), {name: _IMPLEMENTATIONS[name] for name in members}
    )()
    assert not isinstance(incomplete, TransportProtocol)


def test_the_shipped_transport_satisfies_the_protocol(
    agent_config: AgentConfig,
) -> None:
    assert isinstance(HTTPTransport(agent_config), TransportProtocol)
