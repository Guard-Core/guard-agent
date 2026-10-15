import asyncio
import base64
from datetime import datetime, timezone
from typing import Any

import pytest

from guard_agent.buffer import EventBuffer
from guard_agent.encryption import PayloadEncryptor
from guard_agent.models import AgentConfig, SecurityEvent, SecurityMetric
from guard_agent.transport import HTTPTransport

FAKE_TIMEOUT = 3.0


def _event(reason: str = "r") -> SecurityEvent:
    return SecurityEvent(
        timestamp=datetime.now(timezone.utc),
        event_type="ip_banned",
        ip_address="127.0.0.1",
        action_taken="block",
        reason=reason,
    )


def _metric() -> SecurityMetric:
    return SecurityMetric(
        timestamp=datetime.now(timezone.utc), metric_type="request_count", value=1.0
    )


def _buffer(**kw: Any) -> EventBuffer:
    return EventBuffer(
        AgentConfig(api_key="k", endpoint="http://localhost:8000", project_id="p", **kw)
    )


class GuardCoreLikeRedis:
    """set_key fails and, like guard-core's RedisManager, reports the failure
    through the agent's send_event before raising."""

    def __init__(self, buffer: EventBuffer) -> None:
        self.buffer = buffer
        self.calls = 0

    async def set_key(self, *args: Any, **kwargs: Any) -> bool:
        self.calls += 1
        await self.buffer.add_event(_event("redis_error"))
        await self.buffer.add_metric(_metric())
        raise ConnectionError("redis down")

    async def delete(self, *args: Any, **kwargs: Any) -> int:
        return 0

    async def get_key(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def keys(self, *args: Any, **kwargs: Any) -> list[str]:
        return []

    async def initialize(self) -> None:
        return None


@pytest.mark.asyncio
async def test_redis_failure_reporting_through_agent_does_not_deadlock() -> None:
    buf = _buffer()
    redis = GuardCoreLikeRedis(buf)
    await buf.initialize_redis(redis)  # type: ignore[arg-type]
    await asyncio.wait_for(buf.add_event(_event()), FAKE_TIMEOUT)
    await asyncio.wait_for(buf.add_metric(_metric()), FAKE_TIMEOUT)
    await asyncio.wait_for(buf.add_event(_event()), FAKE_TIMEOUT)
    await _settle(buf)
    assert redis.calls == 3
    assert len(buf.event_buffer) == 5
    assert len(buf.metric_buffer) == 4
    assert buf._event_redis_keys == {}


class GatedRedis:
    """set_key blocks on an asyncio.Event, so ordering is deterministic."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.store: dict[str, str] = {}

    async def set_key(self, ns: str, key: str, value: str, ttl: Any = None) -> bool:
        self.entered.set()
        await self.release.wait()
        self.store[key] = value
        return True

    async def delete(self, ns: str, key: str) -> int:
        return int(self.store.pop(key, None) is not None)

    async def get_key(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def keys(self, *args: Any, **kwargs: Any) -> list[str]:
        return []

    async def initialize(self) -> None:
        return None


async def _settle(buf: EventBuffer) -> None:
    await asyncio.wait_for(asyncio.gather(*buf._inflight_persist_tasks), FAKE_TIMEOUT)


@pytest.mark.asyncio
async def test_add_returns_before_slow_redis_completes() -> None:
    buf = _buffer(buffer_size=100)
    redis = GatedRedis()
    await buf.initialize_redis(redis)  # type: ignore[arg-type]
    await asyncio.wait_for(
        asyncio.gather(
            *[buf.add_event(_event()) for _ in range(10)],
            *[buf.add_metric(_metric()) for _ in range(10)],
        ),
        FAKE_TIMEOUT,
    )
    assert len(buf.event_buffer) == 10 and len(buf.metric_buffer) == 10
    assert redis.store == {}
    assert buf._event_redis_keys == {}
    redis.release.set()
    await _settle(buf)
    assert len(redis.store) == 20
    assert len(buf._event_redis_keys) == 10
    assert len(buf._metric_redis_keys) == 10


@pytest.mark.asyncio
async def test_stop_drains_pending_persist() -> None:
    buf = _buffer()
    redis = GatedRedis()
    await buf.initialize_redis(redis)  # type: ignore[arg-type]
    await buf.start_auto_flush()
    await buf.add_event(_event())
    await buf.add_metric(_metric())
    await asyncio.wait_for(redis.entered.wait(), FAKE_TIMEOUT)
    stopper = asyncio.create_task(buf.stop_auto_flush())
    await asyncio.sleep(0)
    assert not stopper.done()
    redis.release.set()
    await asyncio.wait_for(stopper, FAKE_TIMEOUT)
    assert len(redis.store) == 2
    assert not buf._inflight_persist_tasks


@pytest.mark.asyncio
async def test_flush_during_persist_deletes_key_instead_of_orphaning() -> None:
    buf = _buffer()
    redis = GatedRedis()
    await buf.initialize_redis(redis)  # type: ignore[arg-type]
    event, metric = _event(), _metric()
    await buf.add_event(event)
    await buf.add_metric(metric)
    await asyncio.wait_for(redis.entered.wait(), FAKE_TIMEOUT)
    events, ekeys = await buf.flush_events_with_keys()
    metrics, mkeys = await buf.flush_metrics_with_keys()
    assert events == [event] and ekeys == [""]
    assert metrics == [metric] and mkeys == [""]
    redis.release.set()
    await _settle(buf)
    assert redis.store == {}
    assert buf._event_redis_keys == {}
    assert buf._metric_redis_keys == {}


@pytest.mark.asyncio
async def test_requeue_during_persist_tracks_key() -> None:
    buf = _buffer()
    redis = GatedRedis()
    await buf.initialize_redis(redis)  # type: ignore[arg-type]
    await buf.add_event(_event())
    await asyncio.wait_for(redis.entered.wait(), FAKE_TIMEOUT)
    events, keys = await buf.flush_events_with_keys()
    await buf.requeue_events_in_memory(events, keys)
    redis.release.set()
    await _settle(buf)
    assert len(redis.store) == 1
    assert list(buf._event_redis_keys.values()) == list(redis.store)


@pytest.mark.asyncio
async def test_persist_failure_is_not_tracked() -> None:
    buf = _buffer()

    class Failing(GatedRedis):
        async def set_key(self, *a: Any, **k: Any) -> bool:
            raise ConnectionError("down")

    await buf.initialize_redis(Failing())  # type: ignore[arg-type]
    await buf.add_event(_event())
    await buf.add_metric(_metric())
    await _settle(buf)
    assert buf._event_redis_keys == {} and buf._metric_redis_keys == {}
    assert buf.redis_persist_failures == 2


def test_encryptor_accepts_unpadded_key() -> None:
    raw = bytes(range(31, 63)) + b""
    padded = base64.urlsafe_b64encode(raw).decode()
    assert padded.endswith("=") and len(padded) == 44
    a = PayloadEncryptor(padded)
    b = PayloadEncryptor(padded.rstrip("="))
    assert b.verify_key()
    assert a.decrypt(b.encrypt({"x": 1})) == {"x": 1}


def test_encryptor_still_rejects_wrong_length_key() -> None:
    short = base64.urlsafe_b64encode(b"a" * 16).decode().rstrip("=")
    with pytest.raises(Exception, match="Invalid key size"):
        PayloadEncryptor(short)


def test_startup_failure_message_includes_cause() -> None:
    from guard_agent.encryption import EncryptionConfigError

    cfg = AgentConfig(
        api_key="x",
        endpoint="https://example.com",
        project_encryption_key="not-a-valid-key",
    )
    with pytest.raises(EncryptionConfigError, match=r"refusing plaintext.*cause: .+"):
        HTTPTransport(cfg)


def test_encryptor_rejects_non_base64_key() -> None:
    with pytest.raises(Exception, match="Invalid project key format"):
        PayloadEncryptor("A")


@pytest.mark.asyncio
async def test_no_persist_task_without_redis_handler() -> None:
    buf = _buffer()
    await buf.add_event(_event())
    await buf.add_metric(_metric())
    assert not buf._inflight_persist_tasks
    assert len(buf.event_buffer) == 1 and len(buf.metric_buffer) == 1


@pytest.mark.asyncio
async def test_inflight_persists_capped_and_overflow_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    buf = _buffer(buffer_size=3)
    redis = GatedRedis()
    await buf.initialize_redis(redis)  # type: ignore[arg-type]
    for _ in range(4):
        await buf.add_event(_event())
    for _ in range(3):
        await buf.add_metric(_metric())
    assert len(buf._inflight_persist_tasks) == 3
    assert buf.redis_persist_failures == 4
    assert sum("persist backlog" in r.message for r in caplog.records) == 1
    redis.release.set()
    await _settle(buf)
    assert not buf._inflight_persist_tasks
    redis.release.clear()
    for _ in range(4):
        await buf.add_event(_event())
    assert sum("persist backlog" in r.message for r in caplog.records) == 2
    redis.release.set()
    await _settle(buf)


@pytest.mark.asyncio
async def test_item_gone_before_task_starts_is_never_written() -> None:
    buf = _buffer()
    redis = GatedRedis()
    redis.release.set()
    await buf.initialize_redis(redis)  # type: ignore[arg-type]
    await buf.add_event(_event())
    await buf.add_metric(_metric())
    await buf.flush_events_with_keys()
    await buf.flush_metrics_with_keys()
    await _settle(buf)
    assert not redis.entered.is_set()
    assert redis.store == {}


@pytest.mark.asyncio
async def test_overflow_eviction_and_clear_drop_pending_persists() -> None:
    buf = _buffer(buffer_size=2)
    redis = GatedRedis()
    redis.release.set()
    await buf.initialize_redis(redis)  # type: ignore[arg-type]
    first = _event()
    await buf.add_event(first)
    await buf.add_event(_event())
    await buf.add_event(_event())
    assert id(first) not in buf._pending_event_persists
    await buf.add_metric(_metric())
    await buf.clear_buffer()
    assert buf._pending_event_persists == {}
    assert buf._pending_metric_persists == {}
    await _settle(buf)
    assert redis.store == {}
    assert buf._event_redis_keys == {} and buf._metric_redis_keys == {}
    assert buf._pending_event_persists == {}


@pytest.mark.asyncio
async def test_metric_requeue_during_persist_tracks_key() -> None:
    buf = _buffer()
    redis = GatedRedis()
    await buf.initialize_redis(redis)  # type: ignore[arg-type]
    await buf.add_metric(_metric())
    await asyncio.wait_for(redis.entered.wait(), FAKE_TIMEOUT)
    metrics, keys = await buf.flush_metrics_with_keys()
    await buf.requeue_metrics_in_memory(metrics, keys)
    redis.release.set()
    await _settle(buf)
    assert list(buf._metric_redis_keys.values()) == list(redis.store)


@pytest.mark.asyncio
async def test_failed_persists_leave_no_pending_ids() -> None:
    buf = _buffer()

    class Failing(GatedRedis):
        async def set_key(self, *a: Any, **k: Any) -> bool:
            raise ConnectionError("down")

    await buf.initialize_redis(Failing())  # type: ignore[arg-type]
    for _ in range(3):
        await buf.add_event(_event())
        await buf.add_metric(_metric())
    await _settle(buf)
    assert buf.redis_persist_failures == 6
    assert buf._pending_event_persists == {}
    assert buf._pending_metric_persists == {}
