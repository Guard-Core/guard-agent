import asyncio
import inspect
from collections.abc import Coroutine
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from guard_agent._sync_redis_adapter import as_async_redis_handler
from guard_agent.client import GuardAgentHandler
from guard_agent.models import AgentConfig, DynamicRules
from tests.rules_http import expire, make_handler, poll, rules_json

pytestmark = pytest.mark.usefixtures("fresh_agent_singleton")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"success": False, "errors": ["boom"]},
        {"detail": "Not Found"},
        {"rule_id": "only-an-id"},
        {"version": 3},
    ],
)
async def test_a_200_body_that_is_not_a_rule_set_is_not_held(body: dict) -> None:
    handler, seen = make_handler(lambda request: httpx.Response(200, json=body))

    rules = await poll(handler)

    assert rules is None
    assert handler.transport._held_rules is None
    expire(handler)
    await poll(handler)
    assert "last_rule_id" not in seen[1].url.params


@pytest.mark.asyncio
async def test_an_error_body_does_not_replace_the_held_rule_set() -> None:
    answers = [rules_json("held", 7), {"success": False, "errors": ["boom"]}]
    handler, seen = make_handler(
        lambda request: httpx.Response(200, json=answers.pop(0))
    )

    first = await poll(handler)
    expire(handler)
    second = await poll(handler)

    assert first is not None and first.rule_id == "held"
    assert second is not None and second.rule_id == "held"
    held = handler.transport._held_rules
    assert held is not None and (held.rule_id, held.version) == ("held", 7)


@pytest.mark.asyncio
async def test_stop_cancels_and_awaits_the_in_flight_rules_request() -> None:
    handler = GuardAgentHandler(AgentConfig(api_key="k" * 20, project_id="p"))
    started = asyncio.Event()
    events: list[str] = []

    async def slow_fetch() -> DynamicRules | None:
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            events.append("fetch finished")
        return None

    async def record_close() -> None:
        events.append("transport closed")

    with (
        patch.object(handler.transport, "fetch_dynamic_rules", slow_fetch),
        patch.object(handler.transport, "close", record_close),
    ):
        caller = asyncio.create_task(handler.get_dynamic_rules())
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        inflight = handler._rules_inflight
        assert inflight is not None

        await handler.stop()

    assert events == ["fetch finished", "transport closed"]
    assert inflight.cancelled()


class AwaitableReturningHandler:
    """A plain (non-async) handler whose methods hand back awaitables."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get_key(
        self, namespace: str, key: str
    ) -> Coroutine[object, object, str | None]:
        return asyncio.sleep(0, self.store.get(f"{namespace}:{key}"))

    def set_key(
        self, namespace: str, key: str, value: str, ttl: int | None = None
    ) -> Coroutine[object, object, bool | None]:
        self.store[f"{namespace}:{key}"] = value
        return asyncio.sleep(0, True)

    def delete(self, namespace: str, key: str) -> Coroutine[object, object, int | None]:
        removed = self.store.pop(f"{namespace}:{key}", None) is not None
        return asyncio.sleep(0, int(removed))

    def keys(self, pattern: str) -> Coroutine[object, object, list[str] | None]:
        return asyncio.sleep(0, list(self.store))

    def initialize(self) -> Coroutine[object, object, None]:
        return asyncio.sleep(0)

    def get_connection(self) -> object:
        return object()


@pytest.mark.asyncio
async def test_a_plain_function_returning_an_awaitable_is_awaited() -> None:
    handler = AwaitableReturningHandler()
    assert not inspect.iscoroutinefunction(handler.get_key)
    adapted = as_async_redis_handler(handler)

    await adapted.initialize()
    assert await adapted.set_key("ns", "k", "v") is True
    assert await adapted.get_key("ns", "k") == "v"
    assert await adapted.keys("*") == ["ns:k"]
    assert await adapted.delete("ns", "k") == 1


def test_typing_extensions_is_floored_where_typeis_exists() -> None:
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    assert '    "typing-extensions>=4.10.0",\n]' in text
