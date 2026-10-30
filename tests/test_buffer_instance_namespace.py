import asyncio
from datetime import datetime, timezone
from typing import Any

from guard_agent.buffer import EventBuffer
from guard_agent.client import GuardAgentHandler
from guard_agent.models import AgentConfig, SecurityEvent, SecurityMetric


class SharedRedis:
    """One Redis for every buffer of a test, like workers sharing one server."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get_key(self, namespace: str, key: str) -> str | None:
        return self.store.get(f"{namespace}:{key}")

    async def set_key(
        self, namespace: str, key: str, value: str, ttl: int | None = None
    ) -> bool:
        self.store[f"{namespace}:{key}"] = value
        return True

    async def delete(self, namespace: str, key: str) -> int:
        return int(self.store.pop(f"{namespace}:{key}", None) is not None)

    async def keys(self, pattern: str) -> list[str]:
        return [k for k in self.store if k.startswith(pattern.rstrip("*"))]

    async def initialize(self) -> None: ...

    def get_connection(self) -> Any:
        raise AssertionError("the buffer reached for a raw Redis connection")


def _event(reason: str = "r") -> SecurityEvent:
    return SecurityEvent(
        timestamp=datetime.now(timezone.utc),
        event_type="penetration_attempt",
        ip_address="203.0.113.7",
        action_taken="logged",
        reason=reason,
    )


def _metric() -> SecurityMetric:
    return SecurityMetric(
        timestamp=datetime.now(timezone.utc), metric_type="request_count", value=1.0
    )


def _buffer() -> EventBuffer:
    return EventBuffer(AgentConfig(api_key="k1234567890abc"))


async def _worker(redis: SharedRedis, events: int, metrics: int = 0) -> EventBuffer:
    buffer = _buffer()
    await buffer.initialize_redis(redis)
    for index in range(events):
        await buffer.add_event(_event(f"{buffer.instance_id}-{index}"))
    for _ in range(metrics):
        await buffer.add_metric(_metric())
    await asyncio.sleep(0.05)
    return buffer


def _keys_of(redis: SharedRedis, buffer: EventBuffer) -> list[str]:
    return [k for k in redis.store if f":{buffer.instance_id}:" in k]


async def test_a_later_buffer_does_not_load_the_events_another_buffer_holds() -> None:
    redis = SharedRedis()
    first = await _worker(redis, events=3)

    second = _buffer()
    await second.initialize_redis(redis)

    assert len(second.event_buffer) == 0
    assert len(first.event_buffer) == 3
    assert len(redis.store) == 3


async def test_clear_buffer_deletes_only_its_own_persisted_keys() -> None:
    redis = SharedRedis()
    first = await _worker(redis, events=3, metrics=1)
    second = await _worker(redis, events=2, metrics=2)
    first_keys = sorted(_keys_of(redis, first))
    assert len(first_keys) == 4
    assert len(_keys_of(redis, second)) == 4

    await second.clear_buffer()

    assert _keys_of(redis, second) == []
    assert sorted(redis.store) == first_keys


async def test_every_buffer_persists_under_its_own_instance_namespace() -> None:
    redis = SharedRedis()
    first = await _worker(redis, events=1, metrics=1)
    second = await _worker(redis, events=1, metrics=1)

    assert first.instance_id != second.instance_id
    assert sorted(redis.store) == sorted(
        k
        for buffer in (first, second)
        for k in (
            f"agent_events:{buffer.instance_id}:{next(iter(buffer._event_redis_keys.values()))}",
            f"agent_metrics:{buffer.instance_id}:{next(iter(buffer._metric_redis_keys.values()))}",
        )
    )


async def test_a_confirmed_flush_deletes_the_key_from_its_own_namespace() -> None:
    redis = SharedRedis()
    first = await _worker(redis, events=2)
    second = await _worker(redis, events=2)

    events, keys = await second.flush_events_with_keys()
    await second.confirm_event_redis_keys(keys)

    assert len(events) == 2
    assert _keys_of(redis, second) == []
    assert len(_keys_of(redis, first)) == 2


async def test_the_other_instances_are_listed_with_their_counts() -> None:
    redis = SharedRedis()
    dead = await _worker(redis, events=3, metrics=1)
    mine = _buffer()
    await mine.initialize_redis(redis)
    redis.store["agent_events:legacy_event"] = "{}"

    assert await mine.list_persisted_instances() == {
        dead.instance_id: {"events": 3, "metrics": 1}
    }


async def test_recovery_moves_the_events_of_a_named_instance_and_only_that_one() -> (
    None
):
    redis = SharedRedis()
    dead = await _worker(redis, events=3, metrics=1)
    other = await _worker(redis, events=2)
    mine = _buffer()
    await mine.initialize_redis(redis)

    assert await mine.recover_orphaned_instance(dead.instance_id) == (3, 1)

    assert len(mine.event_buffer) == 3
    assert len(mine.metric_buffer) == 1
    assert _keys_of(redis, dead) == []
    assert len(_keys_of(redis, other)) == 2
    assert len(_keys_of(redis, mine)) == 4
    assert sorted(mine._event_redis_keys.values()) == sorted(
        k.split(":")[-1] for k in _keys_of(redis, mine) if "agent_events:" in k
    )

    events, keys = await mine.flush_events_with_keys()
    await mine.confirm_event_redis_keys(keys)
    assert len(events) == 3
    assert len(_keys_of(redis, mine)) == 1


async def test_recovering_this_instance_or_an_unknown_one_adopts_nothing() -> None:
    redis = SharedRedis()
    mine = await _worker(redis, events=2)

    assert await mine.recover_orphaned_instance(mine.instance_id) == (0, 0)
    assert await mine.recover_orphaned_instance("never-existed") == (0, 0)
    assert len(mine.event_buffer) == 2
    assert len(_keys_of(redis, mine)) == 2


async def test_the_handler_exposes_the_listing_and_the_recovery() -> None:
    redis = SharedRedis()
    dead = await _worker(redis, events=2)
    GuardAgentHandler._instance = None
    handler = GuardAgentHandler(AgentConfig(api_key="k1234567890abc"))
    try:
        await handler.initialize_redis(redis)

        assert await handler.list_persisted_instances() == {
            dead.instance_id: {"events": 2, "metrics": 0}
        }
        assert await handler.recover_orphaned_instance(dead.instance_id) == (2, 0)
        assert len(handler.buffer.event_buffer) == 2
    finally:
        GuardAgentHandler._instance = None
