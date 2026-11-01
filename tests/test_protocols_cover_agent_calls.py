from collections.abc import Generator

import pytest

from guard_agent._json_types import JsonValue
from guard_agent.buffer import EventBuffer
from guard_agent.client import GuardAgentHandler
from guard_agent.models import (
    AgentConfig,
    AgentStatus,
    DynamicRules,
    SecurityEvent,
    SecurityMetric,
)
from guard_agent.protocols import (
    BufferProtocol,
    RedisHandlerProtocol,
    TransportProtocol,
)
from guard_agent.transport import HTTPTransport


@pytest.fixture(autouse=True)
def reset_singleton() -> Generator[None, None, None]:
    GuardAgentHandler._instance = None
    yield
    GuardAgentHandler._instance = None


class _TransportFromProtocol:
    """Implements exactly the members `TransportProtocol` declares, no others."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def initialize(self) -> None:
        self.calls.append("initialize")

    async def close(self) -> None:
        self.calls.append("close")

    async def send_events(self, events: list[SecurityEvent]) -> bool:
        return True

    async def send_metrics(self, metrics: list[SecurityMetric]) -> bool:
        return True

    async def send_status(self, status: AgentStatus) -> bool:
        return True

    async def fetch_dynamic_rules(self) -> DynamicRules | None:
        return None

    def get_stats(self) -> dict[str, JsonValue]:
        return {"circuit_breaker_state": "CLOSED"}


class _BufferFromProtocol:
    """Implements exactly the members `BufferProtocol` declares, no others."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.redis_handler: RedisHandlerProtocol | None = None
        self.instance_id = "from-the-protocol"

    async def initialize_redis(self, redis_handler: RedisHandlerProtocol) -> None:
        self.redis_handler = redis_handler

    async def list_persisted_instances(self) -> dict[str, dict[str, int]]:
        return {}

    async def recover_orphaned_instance(self, instance_id: str) -> tuple[int, int]:
        return 0, 0

    async def start_auto_flush(self) -> None:
        self.calls.append("start_auto_flush")

    async def stop_auto_flush(self) -> None:
        self.calls.append("stop_auto_flush")

    def get_stats(self) -> dict[str, JsonValue]:
        return {"events_buffered": 0, "last_flush_time": None}

    async def add_event(self, event: SecurityEvent) -> None:
        return None

    async def add_metric(self, metric: SecurityMetric) -> None:
        return None

    async def flush_events(self) -> list[SecurityEvent]:
        return []

    async def flush_metrics(self) -> list[SecurityMetric]:
        return []

    async def flush_events_with_keys(self) -> tuple[list[SecurityEvent], list[str]]:
        return [], []

    async def flush_metrics_with_keys(self) -> tuple[list[SecurityMetric], list[str]]:
        return [], []

    async def confirm_event_redis_keys(self, keys: list[str]) -> None:
        return None

    async def confirm_metric_redis_keys(self, keys: list[str]) -> None:
        return None

    async def requeue_events_in_memory(
        self, events: list[SecurityEvent], keys: list[str]
    ) -> list[str]:
        return []

    async def requeue_metrics_in_memory(
        self, metrics: list[SecurityMetric], keys: list[str]
    ) -> list[str]:
        return []

    async def get_buffer_size(self) -> int:
        return 0

    async def clear_buffer(self) -> None:
        return None


def _declared(protocol: type) -> set[str]:
    annotations = getattr(protocol, "__annotations__", {})
    methods = {
        name
        for name, member in vars(protocol).items()
        if callable(member) and not name.startswith("_")
    }
    return methods | set(annotations)


def _only_declared(stand_in: type, protocol: type) -> object:
    """An instance holding just the members the protocol declares."""
    declared = _declared(protocol)
    members = {
        name: member for name, member in vars(stand_in).items() if name in declared
    }
    instance = type(f"Only{stand_in.__name__}", (), members)()
    instance.__dict__.update(
        {name: value for name, value in vars(stand_in()).items() if name in declared}
    )
    instance.__dict__["calls"] = []
    return instance


def _implemented(cls: type) -> set[str]:
    return {name for name, member in vars(cls).items() if callable(member)} - {
        "__init__"
    }


def test_the_test_implementations_hold_exactly_the_declared_members() -> None:
    assert _declared(TransportProtocol) == _implemented(_TransportFromProtocol)
    assert _declared(BufferProtocol) - {"redis_handler", "instance_id"} == (
        _implemented(_BufferFromProtocol)
    )
    assert {"redis_handler", "instance_id"} <= _declared(BufferProtocol)


def test_the_stand_ins_satisfy_the_protocols() -> None:
    assert isinstance(_TransportFromProtocol(), TransportProtocol)
    assert isinstance(_BufferFromProtocol(), BufferProtocol)


def test_the_shipped_classes_satisfy_the_protocols(agent_config: AgentConfig) -> None:
    assert isinstance(HTTPTransport(agent_config), TransportProtocol)
    assert isinstance(EventBuffer(agent_config), BufferProtocol)


async def test_an_agent_runs_on_a_transport_and_buffer_built_from_the_protocols(
    agent_config: AgentConfig,
) -> None:
    transport = _only_declared(_TransportFromProtocol, TransportProtocol)
    buffer = _only_declared(_BufferFromProtocol, BufferProtocol)
    agent = GuardAgentHandler(agent_config)
    agent.__dict__["transport"] = transport
    agent.__dict__["buffer"] = buffer

    await agent.start()
    stats = agent.get_stats()
    await agent.stop()

    assert vars(transport)["calls"] == ["initialize", "close"]
    assert vars(buffer)["calls"] == ["start_auto_flush", "stop_auto_flush"]
    assert stats["transport_stats"] == {"circuit_breaker_state": "CLOSED"}
    assert stats["buffer_stats"] == {"events_buffered": 0, "last_flush_time": None}


async def test_an_agent_reads_the_buffer_stand_in_through_the_declared_members_only(
    agent_config: AgentConfig,
) -> None:
    agent = GuardAgentHandler(agent_config)
    agent.__dict__["transport"] = _only_declared(
        _TransportFromProtocol, TransportProtocol
    )
    agent.__dict__["buffer"] = _only_declared(_BufferFromProtocol, BufferProtocol)

    assert await agent.list_persisted_instances() == {}
    assert await agent.recover_orphaned_instance("dead") == (0, 0)
    status = await agent.get_status()
    assert status.status == "healthy"
