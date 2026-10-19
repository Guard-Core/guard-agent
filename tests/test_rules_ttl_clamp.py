from typing import Any

import httpx
import pytest

from guard_agent.models import DynamicRules
from tests.rules_http import make_handler, poll

pytestmark = pytest.mark.usefixtures("fresh_agent_singleton")


@pytest.mark.parametrize(
    ("given", "expected"),
    [(30, 60), (0, 60), (-5, 60), (10**9, 86400), (None, 300), (120, 120)],
)
def test_rules_ttl_is_clamped_never_rejected(given: Any, expected: int) -> None:
    assert DynamicRules(ttl=given).ttl == expected
    assert DynamicRules.model_validate({"ttl": given}).ttl == expected


def test_a_missing_ttl_is_300() -> None:
    assert DynamicRules().ttl == 300
    assert DynamicRules.model_validate({}).ttl == 300


@pytest.mark.asyncio
@pytest.mark.parametrize(("given", "expected"), [(30, 60), (10**9, 86400), (None, 300)])
async def test_a_rules_response_with_an_out_of_range_ttl_still_loads(
    given: Any, expected: int
) -> None:
    body = {"rule_id": "r1", "version": 1, "ttl": given}
    handler, _ = make_handler(lambda request: httpx.Response(200, json=body))

    rules = await poll(handler)

    assert rules is not None and rules.ttl == expected
