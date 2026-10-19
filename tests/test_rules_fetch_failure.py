import httpx
import pytest

from tests.rules_http import make_handler, poll

pytestmark = pytest.mark.usefixtures("fresh_agent_singleton")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 422])
async def test_rules_get_does_not_retry_a_permanent_client_error(status: int) -> None:
    handler, seen = make_handler(lambda request: httpx.Response(status, text="no"))

    assert await poll(handler) is None

    assert len(seen) == 1
