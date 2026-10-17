from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from guard_agent import _transport_lifecycle
from guard_agent.models import AgentConfig, AgentStatus, SecurityEvent
from guard_agent.transport import HTTPTransport
from guard_agent.utils import RateLimiter

SAAS_RULES_PER_MINUTE = 30
SAAS_STATUS_PER_MINUTE = 30
SAAS_API_KEY_BUCKET_PER_MINUTE = 120


def _per_minute(budget: tuple[int, float]) -> float:
    calls, window = budget
    return calls * 60.0 / window


async def _exhaust(limiter: RateLimiter) -> None:
    while await limiter.acquire():
        pass


def _config(retry_attempts: int) -> AgentConfig:
    return AgentConfig(
        api_key="test-api-key",
        endpoint="http://localhost:8000",
        project_id="p",
        retry_attempts=retry_attempts,
        backoff_factor=0.0,
    )


def _status() -> AgentStatus:
    return AgentStatus(
        timestamp=datetime.now(timezone.utc),
        status="healthy",
        uptime=1.0,
        events_sent=0,
        events_failed=0,
        buffer_size=0,
    )


def _event() -> SecurityEvent:
    return SecurityEvent(
        timestamp=datetime.now(timezone.utc),
        event_type="ip_banned",
        ip_address="1.2.3.4",
        action_taken="blocked",
    )


def test_every_budget_stays_below_the_saas_caps() -> None:
    assert _per_minute(_transport_lifecycle.RULES_RATE_BUDGET) < SAAS_RULES_PER_MINUTE
    assert _per_minute(_transport_lifecycle.STATUS_RATE_BUDGET) < SAAS_STATUS_PER_MINUTE
    assert (
        _per_minute(_transport_lifecycle.TELEMETRY_RATE_BUDGET)
        < SAAS_API_KEY_BUCKET_PER_MINUTE
    )


def test_transport_builds_three_independent_limiters_from_the_budgets() -> None:
    transport = HTTPTransport(_config(1))
    limiters = {
        id(transport.rate_limiter),
        id(transport.rules_rate_limiter),
        id(transport.status_rate_limiter),
    }
    assert len(limiters) == 3
    assert transport.rate_limiter.max_calls == 100
    assert transport.rules_rate_limiter.max_calls == 10
    assert transport.status_rate_limiter.max_calls == 10
    assert transport.rules_rate_limiter.time_window == 60.0


def test_a_fork_rebuilds_all_three_limiters() -> None:
    transport = HTTPTransport(_config(1))
    before = (
        transport.rate_limiter,
        transport.rules_rate_limiter,
        transport.status_rate_limiter,
    )
    transport._reset_after_fork()
    after = (
        transport.rate_limiter,
        transport.rules_rate_limiter,
        transport.status_rate_limiter,
    )
    assert all(new is not old for new, old in zip(after, before, strict=True))
    assert [limiter.max_calls for limiter in after] == [100, 10, 10]


@pytest.mark.asyncio
async def test_rule_fetch_is_not_delayed_by_an_exhausted_telemetry_budget() -> None:
    transport = HTTPTransport(_config(1))
    await _exhaust(transport.rate_limiter)

    with (
        patch.object(
            transport, "_make_request", AsyncMock(return_value={"rule_id": "r"})
        ) as request,
        patch("guard_agent._transport_send.asyncio.sleep", AsyncMock()) as sleep,
    ):
        result = await transport._get_with_retry("/api/v1/rules")

    assert result == {"rule_id": "r"}
    sleep.assert_not_awaited()
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_exhausted_rule_budget_waits_without_burning_a_retry_attempt() -> None:
    transport = HTTPTransport(_config(0))
    await _exhaust(transport.rules_rate_limiter)

    async def refill(_: float) -> None:
        transport.rules_rate_limiter.calls.clear()

    with (
        patch.object(
            transport, "_make_request", AsyncMock(return_value={"rule_id": "r"})
        ) as request,
        patch(
            "guard_agent._transport_send.asyncio.sleep", AsyncMock(side_effect=refill)
        ) as sleep,
    ):
        result = await transport._get_with_retry("/api/v1/rules")

    assert result == {"rule_id": "r"}
    sleep.assert_awaited_once()
    assert sleep.await_args is not None and sleep.await_args.args[0] > 0
    request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_repeated_server_failures_never_exceed_the_attempt_budget(
    exhausted: bool,
) -> None:
    transport = HTTPTransport(_config(3))
    if exhausted:
        await _exhaust(transport.rules_rate_limiter)

    async def refill(_: float) -> None:
        transport.rules_rate_limiter.calls.clear()

    with (
        patch.object(
            transport, "_make_request", AsyncMock(side_effect=RuntimeError("503"))
        ) as request,
        patch(
            "guard_agent._transport_send.asyncio.sleep", AsyncMock(side_effect=refill)
        ),
    ):
        result = await transport._get_with_retry("/api/v1/rules")

    assert result is None
    assert request.await_count == 4


@pytest.mark.asyncio
async def test_status_has_its_own_budget() -> None:
    transport = HTTPTransport(_config(1))
    await _exhaust(transport.rate_limiter)

    with (
        patch.object(transport, "_make_request", AsyncMock(return_value={"ok": True})),
        patch("guard_agent._transport_send.asyncio.sleep", AsyncMock()) as sleep,
    ):
        assert await transport.send_status(_status()) is True
    sleep.assert_not_awaited()

    await _exhaust(transport.status_rate_limiter)
    with (
        patch.object(transport, "_make_request", AsyncMock(return_value={"ok": True})),
        patch("guard_agent._transport_send.asyncio.sleep", AsyncMock()) as sleep,
    ):
        await transport.send_status(_status())
    sleep.assert_awaited()


@pytest.mark.asyncio
async def test_telemetry_keeps_its_retry_semantics_when_its_budget_is_exhausted() -> (
    None
):
    transport = HTTPTransport(_config(0))
    await _exhaust(transport.rate_limiter)

    with (
        patch.object(
            transport, "_make_request", AsyncMock(return_value={"ok": True})
        ) as request,
        patch("guard_agent._transport_send.asyncio.sleep", AsyncMock()) as sleep,
    ):
        assert await transport.send_events([_event()]) is False

    sleep.assert_awaited_once()
    request.assert_not_awaited()
