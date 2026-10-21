import gzip
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from guard_agent.client import GuardAgentHandler
from guard_agent.models import AgentConfig, SecurityEvent

ATTRIBUTION = {
    "matched_rule_type": "dynamic",
    "matched_rule_id": "rule-wp23",
    "matched_rule_overlaps_static": True,
    "correlation_id": "0b7f9a8e-3f1c-4a0e-9d3e-5d1d9a3c2b11",
    "threat_categories": ["xss", "sqli"],
    "category": "xss",
}


def _client() -> AsyncMock:
    response = AsyncMock()
    response.status_code = 200
    response.json = MagicMock(return_value={"status": "ok"})
    response.text = "OK"
    response.headers = {}
    response.url = "http://localhost:8000/api/v1/events"
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.is_closed = False
    client.aclose = AsyncMock()
    return client


def _posted_events(client: AsyncMock) -> list[dict[str, Any]]:
    [call] = client.post.call_args_list
    body = call.kwargs["content"]
    if call.kwargs["headers"].get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    events: list[dict[str, Any]] = json.loads(body)["events"]
    return events


@pytest.mark.asyncio
async def test_event_attribution_metadata_reaches_the_wire_unchanged(
    agent_config: AgentConfig,
    fresh_agent_singleton: None,
) -> None:
    handler = GuardAgentHandler(agent_config)
    client = _client()
    handler.transport._client = client

    await handler.send_event(
        SecurityEvent(
            timestamp=datetime.now(timezone.utc),
            event_type="penetration_attempt",
            ip_address="203.0.113.7",
            action_taken="request_blocked",
            endpoint="/search",
            metadata={**ATTRIBUTION, "trigger_info": "Request body"},
        )
    )
    await handler.flush_buffer()

    [event] = _posted_events(client)
    assert event["endpoint"] == "/search"
    assert {k: event["metadata"][k] for k in ATTRIBUTION} == ATTRIBUTION


@pytest.mark.asyncio
async def test_a_duck_typed_event_keeps_the_attribution_metadata(
    agent_config: AgentConfig,
    fresh_agent_singleton: None,
) -> None:
    handler = GuardAgentHandler(agent_config)
    client = _client()
    handler.transport._client = client
    duck = type(
        "SecurityEvent",
        (),
        {
            "timestamp": datetime.now(timezone.utc),
            "event_type": "pattern_detected",
            "ip_address": "203.0.113.7",
            "action_taken": "threat_detected",
            "reason": "Threat detected in query",
            "endpoint": "/search",
            "metadata": dict(ATTRIBUTION),
        },
    )()

    await handler.send_event(duck)
    await handler.flush_buffer()

    [event] = _posted_events(client)
    assert event["endpoint"] == "/search"
    assert {k: event["metadata"][k] for k in ATTRIBUTION} == ATTRIBUTION
