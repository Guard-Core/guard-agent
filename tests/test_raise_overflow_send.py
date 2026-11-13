"""buffer_overflow_policy="raise" reaches the caller of send_event and send_metric.

The ingest methods caught every exception, so BufferFullError never left them and
the documented policy did nothing. Every other policy is unchanged, and under the
engine the raise never fails a request: the engine guards each send site.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import datetime, timezone
from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from guard import SecurityConfig, SecurityMiddleware

from guard_agent.client import GuardAgentHandler, SyncGuardAgentHandler
from guard_agent.exceptions import BufferFullError
from guard_agent.models import AgentConfig, SecurityEvent, SecurityMetric

Policy = Literal["drop", "block", "raise"]


def _event() -> SecurityEvent:
    return SecurityEvent(
        timestamp=datetime.now(timezone.utc),
        event_type="ip_banned",
        ip_address="203.0.113.7",
        action_taken="block",
        reason="test",
    )


def _metric() -> SecurityMetric:
    return SecurityMetric(
        timestamp=datetime.now(timezone.utc), metric_type="request_count", value=1.0
    )


def _config(policy: Policy) -> AgentConfig:
    return AgentConfig(
        api_key="test-api-key",
        endpoint="http://localhost:8000",
        project_id="test-project",
        buffer_size=1,
        flush_interval=3600,
        buffer_overflow_policy=policy,
        buffer_block_timeout=0.05,
    )


@pytest.fixture(autouse=True)
def _fresh_singletons() -> Generator[None, None, None]:
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


async def test_raise_policy_send_event_reaches_the_caller() -> None:
    handler = GuardAgentHandler(_config("raise"))
    await handler.send_event(_event())
    with pytest.raises(BufferFullError, match="Event buffer full"):
        await handler.send_event(_event())
    assert len(handler.buffer.event_buffer) == 1


async def test_raise_policy_send_metric_reaches_the_caller() -> None:
    handler = GuardAgentHandler(_config("raise"))
    await handler.send_metric(_metric())
    with pytest.raises(BufferFullError, match="Metric buffer full"):
        await handler.send_metric(_metric())


def test_raise_policy_reaches_the_sync_handler_caller() -> None:
    handler = SyncGuardAgentHandler(_config("raise"))
    handler.send_event(_event())
    with pytest.raises(BufferFullError, match="Event buffer full"):
        handler.send_event(_event())


@pytest.mark.parametrize("policy", ["drop", "block"])
async def test_other_policies_still_swallow_a_full_buffer(policy: Policy) -> None:
    handler = GuardAgentHandler(_config(policy))
    await handler.send_event(_event())
    await handler.send_event(_event())
    await handler.send_metric(_metric())
    await handler.send_metric(_metric())
    assert len(handler.buffer.event_buffer) == 1
    assert handler.buffer.events_dropped == 1


async def test_a_non_overflow_failure_is_still_swallowed_under_raise() -> None:
    handler = GuardAgentHandler(_config("raise"))
    await handler.send_event(object())
    assert len(handler.buffer.event_buffer) == 0


def test_engine_request_answers_normally_when_the_raise_policy_buffer_is_full(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = FastAPI()
    config = SecurityConfig(
        enable_agent=True,
        enable_redis=False,
        agent_api_key="test-api-key-1234567890",
        agent_endpoint="http://127.0.0.1:9",
        agent_project_id="test-project",
        agent_buffer_size=1,
        agent_flush_interval=3600,
        agent_buffer_overflow_policy="raise",
        passive_mode=True,
        ban_on_hostile_signals=False,
        rate_limit=100,
        whitelist_access_only=False,
    )
    app.add_middleware(SecurityMiddleware, config=config)

    @app.get("/")
    def root() -> dict[str, bool]:
        return {"ok": True}

    with caplog.at_level(logging.ERROR), TestClient(app) as client:
        statuses = [client.get("/").status_code for _ in range(4)]

    assert statuses == [200, 200, 200, 200]
    raised = [
        record.exc_info[1]
        for record in caplog.records
        if record.name.startswith("guard_core") and record.exc_info
    ]
    assert any(isinstance(error, BufferFullError) for error in raised), (
        "the engine never saw BufferFullError: the raise policy is still inert"
    )
