import asyncio
import logging
import time
from datetime import datetime, timezone

import pytest
from pytest import LogCaptureFixture

from guard_agent.buffer import EventBuffer
from guard_agent.models import AgentConfig, SecurityEvent, SecurityMetric


def _event(reason: str = "r") -> SecurityEvent:
    return SecurityEvent(
        timestamp=datetime.now(timezone.utc),
        event_type="ip_banned",
        ip_address="1.2.3.4",
        action_taken="blocked",
        reason=reason,
    )


def _metric() -> SecurityMetric:
    return SecurityMetric(
        timestamp=datetime.now(timezone.utc),
        metric_type="request_count",
        value=1.0,
    )


def test_buffer_block_timeout_defaults_to_one_second() -> None:
    assert AgentConfig(api_key="k").buffer_block_timeout == 1.0


def test_buffer_block_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError):
        AgentConfig(api_key="k", buffer_block_timeout=0)


@pytest.mark.asyncio
async def test_block_policy_event_waits_at_most_the_timeout_then_drops(
    caplog: LogCaptureFixture,
) -> None:
    config = AgentConfig(
        api_key="k",
        buffer_size=1,
        buffer_overflow_policy="block",
        buffer_block_timeout=0.1,
    )
    buffer = EventBuffer(config)
    kept = _event("kept")
    await buffer.add_event(kept)

    with caplog.at_level(logging.WARNING):
        start = time.perf_counter()
        await asyncio.wait_for(buffer.add_event(_event("late")), timeout=3)
        elapsed = time.perf_counter() - start

    assert 0.09 <= elapsed < 0.5
    assert list(buffer.event_buffer) == [kept]
    assert buffer.events_dropped == 1
    assert any("buffer_block_timeout" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_block_policy_metric_waits_at_most_the_timeout_then_drops(
    caplog: LogCaptureFixture,
) -> None:
    config = AgentConfig(
        api_key="k",
        buffer_size=1,
        buffer_overflow_policy="block",
        buffer_block_timeout=0.1,
    )
    buffer = EventBuffer(config)
    kept = _metric()
    await buffer.add_metric(kept)

    with caplog.at_level(logging.WARNING):
        start = time.perf_counter()
        await asyncio.wait_for(buffer.add_metric(_metric()), timeout=3)
        elapsed = time.perf_counter() - start

    assert 0.09 <= elapsed < 0.5
    assert list(buffer.metric_buffer) == [kept]
    assert buffer.metrics_dropped == 1
    assert any("buffer_block_timeout" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_block_policy_timeout_warning_is_sampled(
    caplog: LogCaptureFixture,
) -> None:
    config = AgentConfig(
        api_key="k",
        buffer_size=1,
        buffer_overflow_policy="block",
        buffer_block_timeout=0.01,
    )
    buffer = EventBuffer(config)
    await buffer.add_event(_event())
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            await buffer.add_event(_event())
    assert buffer.events_dropped == 3
    assert (
        len([r for r in caplog.records if "buffer_block_timeout" in r.getMessage()])
        == 1
    )


@pytest.mark.asyncio
async def test_block_policy_still_admits_when_space_frees_within_timeout() -> None:
    config = AgentConfig(
        api_key="k",
        buffer_size=1,
        buffer_overflow_policy="block",
        buffer_block_timeout=2.0,
    )
    buffer = EventBuffer(config)
    await buffer.add_event(_event("first"))
    late = _event("late")
    pending = asyncio.create_task(buffer.add_event(late))
    await asyncio.sleep(0.05)
    await buffer.flush_events()
    await asyncio.wait_for(pending, timeout=2)
    assert late in list(buffer.event_buffer)
    assert buffer.events_dropped == 0


@pytest.mark.asyncio
async def test_block_policy_metric_timeout_warning_is_sampled(
    caplog: LogCaptureFixture,
) -> None:
    config = AgentConfig(
        api_key="k",
        buffer_size=1,
        buffer_overflow_policy="block",
        buffer_block_timeout=0.01,
    )
    buffer = EventBuffer(config)
    await buffer.add_metric(_metric())
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            await buffer.add_metric(_metric())
    assert buffer.metrics_dropped == 3
    assert (
        len([r for r in caplog.records if "buffer_block_timeout" in r.getMessage()])
        == 1
    )
