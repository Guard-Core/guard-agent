import asyncio
from datetime import datetime, timezone

import pytest

from guard_agent.buffer import EventBuffer
from guard_agent.models import AgentConfig, SecurityEvent


def _make_config(
    buffer_size: int = 10,
    flush_interval: float = 10.0,
    high_watermark_ratio: float = 0.8,
    max_concurrent_flushes: int = 1,
) -> AgentConfig:
    return AgentConfig(
        api_key="test-key-api-key-1",
        endpoint="http://localhost:8000",
        buffer_size=buffer_size,
        flush_interval=int(flush_interval),
        high_watermark_ratio=high_watermark_ratio,
        max_concurrent_flushes=max_concurrent_flushes,
    )


def _make_event() -> SecurityEvent:
    return SecurityEvent(
        timestamp=datetime.now(timezone.utc),
        event_type="ip_blocked",
        ip_address="1.2.3.4",
        method="GET",
        status_code=403,
    )


@pytest.mark.asyncio
async def test_buffer_at_high_watermark_triggers_real_flush() -> None:
    flush_count: list[int] = []

    async def fake_flush() -> None:
        flush_count.append(1)

    config = _make_config(buffer_size=10, high_watermark_ratio=0.8)
    buf = EventBuffer(config, flush_callback=fake_flush)
    await buf.start()
    try:
        for _ in range(9):
            await buf.add_event(_make_event())
        await asyncio.sleep(0.05)
    finally:
        await buf.stop()

    assert flush_count, "high-watermark must trigger an early flush"


@pytest.mark.asyncio
async def test_concurrent_flush_calls_are_capped() -> None:
    counter = {"n": 0, "max": 0}

    async def slow_flush() -> None:
        counter["n"] += 1
        counter["max"] = max(counter["max"], counter["n"])
        await asyncio.sleep(0.05)
        counter["n"] -= 1

    config = _make_config(
        buffer_size=4,
        flush_interval=10,
        high_watermark_ratio=0.5,
        max_concurrent_flushes=1,
    )
    buf = EventBuffer(config, flush_callback=slow_flush)
    await buf.start()
    try:
        for _ in range(20):
            await buf.add_event(_make_event())
            await asyncio.sleep(0)
        await asyncio.sleep(0.5)
    finally:
        await buf.stop()

    assert counter["max"] <= 1


@pytest.mark.asyncio
async def test_no_flush_when_below_watermark() -> None:
    flush_count: list[int] = []

    async def fake_flush() -> None:
        flush_count.append(1)

    config = _make_config(buffer_size=10, high_watermark_ratio=0.8)
    buf = EventBuffer(config, flush_callback=fake_flush)
    await buf.start()
    try:
        for _ in range(5):
            await buf.add_event(_make_event())
        await asyncio.sleep(0.05)
    finally:
        await buf.stop()

    assert not flush_count, "below watermark must not trigger early flush"


@pytest.mark.asyncio
async def test_flush_if_needed_no_callback_does_not_raise() -> None:
    config = _make_config(buffer_size=10, high_watermark_ratio=0.5)
    buf = EventBuffer(config, flush_callback=None)
    await buf.start()
    try:
        for _ in range(6):
            await buf.add_event(_make_event())
        await asyncio.sleep(0.05)
    finally:
        await buf.stop()


@pytest.mark.asyncio
async def test_flush_if_needed_skipped_when_semaphore_locked() -> None:
    call_count: list[int] = []
    gate: asyncio.Event = asyncio.Event()

    async def blocking_flush() -> None:
        call_count.append(1)
        await gate.wait()

    config = _make_config(
        buffer_size=4, high_watermark_ratio=0.5, max_concurrent_flushes=1
    )
    buf = EventBuffer(config, flush_callback=blocking_flush)
    await buf.start()
    try:
        for _ in range(10):
            await buf.add_event(_make_event())
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
    finally:
        gate.set()
        await buf.stop()

    assert len(call_count) == 1, "semaphore must cap concurrent flushes to 1"


@pytest.mark.asyncio
async def test_start_and_stop_aliases() -> None:
    config = _make_config()
    buf = EventBuffer(config)
    await buf.start()
    assert buf._running
    await buf.stop()
    assert not buf._running
