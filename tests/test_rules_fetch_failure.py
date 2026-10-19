import httpx
import pytest

from guard_agent.models import DynamicRules
from tests.rules_http import Responder, expire, make_handler, poll

pytestmark = pytest.mark.usefixtures("fresh_agent_singleton")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 422])
async def test_rules_get_does_not_retry_a_permanent_client_error(status: int) -> None:
    handler, seen = make_handler(lambda request: httpx.Response(status, text="no"))

    assert await poll(handler) is None

    assert len(seen) == 1


def _failing_responses() -> list[tuple[str, Responder]]:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    return [
        ("5xx", lambda request: httpx.Response(503, text="down")),
        ("timeout", timeout),
        ("permanent-4xx", lambda request: httpx.Response(404, text="gone")),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["5xx", "timeout", "permanent-4xx"])
async def test_failed_fetch_returns_the_cached_rules(name: str) -> None:
    responder = dict(_failing_responses())[name]
    handler, _ = make_handler(responder)
    held = DynamicRules(rule_id="held", version=7)
    handler._cached_rules = held
    expire(handler)

    assert await poll(handler) is held


@pytest.mark.asyncio
async def test_failed_fetch_without_held_rules_is_still_none() -> None:
    handler, _ = make_handler(lambda request: httpx.Response(503, text="down"))

    assert await poll(handler) is None
