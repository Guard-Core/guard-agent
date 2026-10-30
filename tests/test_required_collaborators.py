import base64

import pytest

from guard_agent.buffer import EventBuffer
from guard_agent.exceptions import GuardAgentError
from guard_agent.models import AgentConfig
from guard_agent.transport import HTTPTransport


def _config() -> AgentConfig:
    return AgentConfig(api_key="k", endpoint="http://localhost:8000", project_id="p")


async def test_redis_loaders_raise_when_handler_missing() -> None:
    buffer = EventBuffer(_config())
    for call in (
        buffer._adopt_events_from_instance("dead"),
        buffer._adopt_metrics_from_instance("dead"),
        buffer._load_one_event_from_redis("agent_events:dead:a", "agent_events:dead"),
        buffer._load_one_metric_from_redis(
            "agent_metrics:dead:a", "agent_metrics:dead"
        ),
        buffer.list_persisted_instances(),
        buffer._delete_matching_redis_keys("agent_events", "agent_events:*"),
    ):
        with pytest.raises(GuardAgentError, match="Redis handler is not initialized"):
            await call


async def test_dispatch_paths_raise_when_client_missing() -> None:
    key = base64.urlsafe_b64encode(b"a" * 32).decode()
    transport = HTTPTransport(
        _config().model_copy(update={"project_encryption_key": key})
    )
    transport._client = None
    for call in (
        transport._post_encrypted({"batch_id": "b"}),
        transport._dispatch_request("GET", "/x", "http://localhost:8000/x", None),
        transport._post_unencrypted("http://localhost:8000/x", {"a": 1}),
    ):
        with pytest.raises(GuardAgentError, match="Failed to initialize HTTP client"):
            await call
