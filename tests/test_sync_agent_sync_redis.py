from __future__ import annotations

import json
import threading
from collections.abc import Generator
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from guard_agent.client import GuardAgentHandler, SyncGuardAgentHandler
from guard_agent.models import AgentConfig, SecurityEvent


class SyncFakeRedis:
    """The shape of guard-core's sync redis handler: plain methods, no awaitables."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.threads: set[int] = set()
        self.initialized = False

    def get_key(self, namespace: str, key: str) -> str | None:
        self.threads.add(threading.get_ident())
        return self.store.get(f"{namespace}:{key}")

    def set_key(
        self, namespace: str, key: str, value: str, ttl: int | None = None
    ) -> bool | None:
        self.threads.add(threading.get_ident())
        self.store[f"{namespace}:{key}"] = value
        return True

    def delete(self, namespace: str, key: str) -> int | None:
        self.threads.add(threading.get_ident())
        return int(self.store.pop(f"{namespace}:{key}", None) is not None)

    def keys(self, pattern: str) -> list[str] | None:
        self.threads.add(threading.get_ident())
        return [k for k in self.store if k.startswith(pattern.rstrip("*"))]

    def initialize(self) -> None:
        self.initialized = True

    def get_connection(self) -> object:
        return self


def _recording_transport() -> AsyncMock:
    transport = AsyncMock()
    transport.send_events = AsyncMock(return_value=True)
    transport.send_metrics = AsyncMock(return_value=True)
    return transport


def _event() -> SecurityEvent:
    return SecurityEvent(
        timestamp=datetime.now(timezone.utc),
        event_type="ip_banned",
        ip_address="1.2.3.4",
        action_taken="banned",
        reason="wp207",
    )


@pytest.fixture(autouse=True)
def reset_singletons() -> Generator[None, None, None]:
    GuardAgentHandler._instance = None
    SyncGuardAgentHandler._instance = None
    yield
    sync_instance: SyncGuardAgentHandler | None = SyncGuardAgentHandler._instance
    GuardAgentHandler._instance = None
    SyncGuardAgentHandler._instance = None
    if sync_instance is not None and not sync_instance._loop.is_closed():
        sync_instance._loop.call_soon_threadsafe(sync_instance._loop.stop)
        sync_instance._thread.join(timeout=2)
        sync_instance._loop.close()


def test_sync_redis_handler_conforms_to_the_sync_protocol() -> None:
    from guard_agent.protocols import SyncRedisHandlerProtocol

    fake: SyncRedisHandlerProtocol = SyncFakeRedis()
    assert isinstance(fake, SyncRedisHandlerProtocol)


def test_sync_redis_loads_persists_tracks_and_deletes(
    agent_config: AgentConfig,
) -> None:
    redis = SyncFakeRedis()
    persisted = _event()
    redis.store["agent_events:old"] = json.dumps(persisted.model_dump(mode="json"))
    handler = SyncGuardAgentHandler(agent_config)
    transport = _recording_transport()
    handler._inner.transport = transport

    handler.initialize_redis(redis)

    buffer = handler._inner.buffer
    assert len(buffer.event_buffer) == 1
    assert buffer.redis_persist_failures == 0

    handler.send_event(_event())

    assert buffer.redis_persist_failures == 0
    assert len(redis.store) == 2
    assert len(buffer._event_redis_keys) == 2

    handler.flush_buffer()

    sent = transport.send_events.await_args.args[0]
    assert len(sent) == 2
    assert redis.store == {}
    assert buffer._event_redis_keys == {}


def test_sync_redis_calls_run_off_the_agent_loop_thread(
    agent_config: AgentConfig,
) -> None:
    redis = SyncFakeRedis()
    handler = SyncGuardAgentHandler(agent_config)
    handler._inner.transport = _recording_transport()

    handler.initialize_redis(redis)
    handler.send_event(_event())

    assert redis.threads
    assert handler._thread.ident not in redis.threads
    assert threading.get_ident() not in redis.threads


def test_async_redis_handler_still_passes_through_unchanged(
    agent_config: AgentConfig, mock_redis_handler: AsyncMock
) -> None:
    handler = SyncGuardAgentHandler(agent_config)
    handler._inner.buffer = AsyncMock()

    handler.initialize_redis(mock_redis_handler)

    assert handler._inner.redis_handler is mock_redis_handler
    handler._inner.buffer.initialize_redis.assert_awaited_once_with(mock_redis_handler)


@pytest.mark.asyncio
async def test_adapter_forwards_every_protocol_method_to_the_sync_handler() -> None:
    from guard_agent._sync_redis_adapter import AsyncRedisFromSync

    redis = SyncFakeRedis()
    adapter = AsyncRedisFromSync(redis)

    await adapter.initialize()
    assert await adapter.set_key("ns", "k", "v", 5) is True
    assert await adapter.get_key("ns", "k") == "v"
    assert await adapter.keys("ns:*") == ["ns:k"]
    assert await adapter.delete("ns", "k") == 1
    assert redis.initialized is True
    assert adapter.get_connection() is redis
