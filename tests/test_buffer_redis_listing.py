import fnmatch
import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, cast

import pytest
from redis.asyncio import Redis

from guard_agent.buffer import EventBuffer
from guard_agent.models import AgentConfig, SecurityEvent, SecurityMetric

PREFIX = "wp16agent:"
REDIS_URL = os.getenv("REDIS_URL") or "redis://localhost:6379"


def _buffer() -> EventBuffer:
    return EventBuffer(
        AgentConfig(api_key="k", endpoint="http://localhost:8000", project_id="p")
    )


def _event_payload() -> str:
    return SecurityEvent(
        timestamp=datetime.now(timezone.utc),
        event_type="ip_banned",
        ip_address="127.0.0.1",
        action_taken="block",
    ).model_dump_json()


def _metric_payload() -> str:
    return SecurityMetric(
        timestamp=datetime.now(timezone.utc), metric_type="request_count", value=1.0
    ).model_dump_json()


class HandlerOnlyRedis:
    """Offers the handler surface and nothing else: a cleanup or load that reaches
    for a raw connection (and so for a raw KEYS) fails."""

    def __init__(self, store: dict[str, str]) -> None:
        self.store = store
        self.key_patterns: list[str] = []

    async def keys(self, pattern: str) -> list[str]:
        self.key_patterns.append(pattern)
        return sorted(
            key for key in self.store if fnmatch.fnmatch(key, f"{PREFIX}{pattern}")
        )

    async def get_key(self, namespace: str, key: str) -> str | None:
        return self.store.get(f"{PREFIX}{namespace}:{key}")

    async def set_key(
        self, namespace: str, key: str, value: str, ttl: int | None = None
    ) -> bool:
        self.store[f"{PREFIX}{namespace}:{key}"] = value
        return True

    async def delete(self, namespace: str, key: str) -> int:
        return int(self.store.pop(f"{PREFIX}{namespace}:{key}", None) is not None)

    def get_connection(self) -> Any:
        raise AssertionError("the buffer reached for a raw Redis connection")


def _seeded_store() -> dict[str, str]:
    return {
        f"{PREFIX}agent_events:event_1": _event_payload(),
        f"{PREFIX}agent_events:event_2": _event_payload(),
        f"{PREFIX}agent_metrics:metric_1": _metric_payload(),
        f"{PREFIX}other:keep": "1",
    }


async def test_the_startup_load_lists_through_the_handler_keys() -> None:
    redis = HandlerOnlyRedis(_seeded_store())
    buffer = _buffer()

    await buffer.initialize_redis(cast(Any, redis))

    assert redis.key_patterns == ["agent_events:*", "agent_metrics:*"]
    assert len(buffer.event_buffer) == 2
    assert len(buffer.metric_buffer) == 1


async def test_every_cleanup_lists_through_the_handler_keys() -> None:
    redis = HandlerOnlyRedis(_seeded_store())
    buffer = _buffer()
    buffer.redis_handler = cast(Any, redis)

    await buffer._clear_events_from_redis(1)
    await buffer._clear_metrics_from_redis(1)
    assert sorted(redis.store) == [
        f"{PREFIX}agent_events:event_2",
        f"{PREFIX}other:keep",
    ]

    await buffer._clear_redis_buffers()

    assert list(redis.store) == [f"{PREFIX}other:keep"]
    assert redis.key_patterns == [
        "agent_events:*",
        "agent_metrics:*",
        "agent_events:*",
        "agent_metrics:*",
    ]


def _engine_lists_with_scan() -> bool:
    from guard_core.handlers import redis_handler

    return hasattr(redis_handler, "scan_unlink")


class NoKeysConnection:
    def __init__(self, conn: Redis) -> None:
        self._conn = conn

    async def keys(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the engine handler issued KEYS on the shared Redis")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


async def _clear(client: Redis) -> None:
    for key in [k async for k in client.scan_iter(match=f"{PREFIX}*")]:
        await client.delete(key)


@pytest.fixture
async def conn() -> AsyncGenerator[Redis, None]:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    await _clear(client)
    yield client
    await _clear(client)
    await client.aclose()


@pytest.mark.skipif(
    not _engine_lists_with_scan(),
    reason="the installed guard-core still lists Redis keys with KEYS",
)
async def test_the_engine_handler_the_buffer_gets_lists_with_scan(
    conn: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    from guard_core.handlers.redis_handler import RedisManager
    from guard_core.models import SecurityConfig

    manager = RedisManager(
        SecurityConfig(
            enable_redis=True,
            redis_url=REDIS_URL,
            redis_prefix=PREFIX,
            ban_on_hostile_signals=False,
            rate_limit=100,
            whitelist_access_only=False,
        )
    )

    @asynccontextmanager
    async def no_keys_connection() -> AsyncIterator[NoKeysConnection]:
        yield NoKeysConnection(conn)

    monkeypatch.setattr(manager, "get_connection", no_keys_connection)
    for key, value in _seeded_store().items():
        await conn.set(key, value)
    buffer = _buffer()
    buffer.redis_handler = manager

    await buffer._load_from_redis()
    assert len(buffer.event_buffer) == 2
    assert len(buffer.metric_buffer) == 1

    await buffer._clear_redis_buffers()

    left = sorted([k async for k in conn.scan_iter(match=f"{PREFIX}*")])
    assert left == [f"{PREFIX}other:keep"]
