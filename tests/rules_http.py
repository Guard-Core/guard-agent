from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from guard_agent.client import GuardAgentHandler
from guard_agent.models import AgentConfig, DynamicRules
from guard_agent.transport import HTTPTransport
from guard_agent.utils import RateLimiter

Responder = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def rules_json(rule_id: str = "r1", version: int = 1, ttl: int = 300) -> dict:
    return {"rule_id": rule_id, "version": version, "ttl": ttl}


def make_handler(
    responder: Responder,
) -> tuple[GuardAgentHandler, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    async def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = responder(request)
        if isinstance(answer, httpx.Response):
            return answer
        return await answer

    config = AgentConfig(
        api_key="test-api-key-1234",
        endpoint="http://saas.test",
        project_id="p",
        retry_attempts=3,
        backoff_factor=0.001,
    )
    transport = HTTPTransport(config)
    # The client budget is real time (10 per 60 s); replays run faster than that.
    transport.rules_rate_limiter = RateLimiter(10**6, 60.0)
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    handler = GuardAgentHandler(config)
    handler.transport = transport
    return handler, seen


def expire(handler: GuardAgentHandler) -> None:
    handler._rules_last_update = 0


async def poll(handler: GuardAgentHandler) -> DynamicRules | None:
    with patch("guard_agent._transport_send.asyncio.sleep", AsyncMock()):
        return await handler.get_dynamic_rules()


@contextmanager
def fake_clock(clock: list[float]) -> Iterator[None]:
    """Replace guard_agent's own clock (not asyncio's) with clock[0]."""
    stub = SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])
    with (
        patch("guard_agent._client_loops.time", stub),
        patch("guard_agent._client_status.time", stub),
    ):
        yield
