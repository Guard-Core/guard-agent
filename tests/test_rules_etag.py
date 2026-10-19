import asyncio
from unittest.mock import patch

import httpx
import pytest

from guard_agent.models import AgentConfig
from guard_agent.transport import HTTPTransport
from tests.rules_http import rules_json

pytestmark = pytest.mark.usefixtures("fresh_agent_singleton")


@pytest.mark.asyncio
async def test_overlapping_gets_keep_each_etag_with_its_own_rules() -> None:
    config = AgentConfig(
        api_key="test-api-key-1234",
        endpoint="http://saas.test",
        retry_attempts=0,
        backoff_factor=0.001,
    )
    transport = HTTPTransport(config)
    second_has_answered = asyncio.Event()
    arrivals: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        arrivals.append("x")
        if len(arrivals) == 1:
            await second_has_answered.wait()
            return httpx.Response(
                200, json=rules_json("first", 1), headers={"ETag": "e-first"}
            )
        second_has_answered.set()
        return httpx.Response(
            200, json=rules_json("second", 1), headers={"ETag": "e-second"}
        )

    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    real_handle = transport._handle_response

    async def yielding_handle(response: httpx.Response) -> dict | bool:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return await real_handle(response)

    with patch.object(transport, "_handle_response", yielding_handle):
        await asyncio.gather(
            transport.fetch_dynamic_rules(), transport.fetch_dynamic_rules()
        )

    held = transport._held_rules
    assert held is not None
    assert transport._held_rules_etag == f"e-{held.rule_id}"


@pytest.mark.asyncio
async def test_a_get_response_carries_the_etag_it_came_with() -> None:
    config = AgentConfig(api_key="test-api-key-1234", endpoint="http://saas.test")
    transport = HTTPTransport(config)
    transport._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=rules_json(), headers={"ETag": '"abc"'}
            )
        )
    )

    response = await transport._make_request("GET", "/api/v1/rules", None)

    assert getattr(response, "etag", None) == '"abc"'
    assert transport.__dict__.get("_last_get_etag") is None


@pytest.mark.asyncio
async def test_a_get_answered_with_no_json_body_is_passed_through() -> None:
    config = AgentConfig(api_key="test-api-key-1234", endpoint="http://saas.test")
    transport = HTTPTransport(config)
    transport._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(201))
    )

    assert await transport._make_request("GET", "/api/v1/rules", None) is True
