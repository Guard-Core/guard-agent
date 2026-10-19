import httpx
import pytest

from tests.rules_http import expire, make_handler, poll, rules_json

pytestmark = pytest.mark.usefixtures("fresh_agent_singleton")


@pytest.mark.asyncio
async def test_first_poll_sends_no_conditional_request() -> None:
    handler, seen = make_handler(lambda request: httpx.Response(200, json=rules_json()))

    await poll(handler)

    assert "if-none-match" not in seen[0].headers
    assert "last_rule_id" not in seen[0].url.params
    assert "last_rule_version" not in seen[0].url.params


@pytest.mark.asyncio
async def test_next_poll_is_conditional_and_a_304_keeps_the_cached_rules() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"abc"':
            return httpx.Response(304)
        return httpx.Response(200, json=rules_json("r1", 4), headers={"ETag": '"abc"'})

    handler, seen = make_handler(respond)
    first = await poll(handler)
    expire(handler)

    second = await poll(handler)

    assert second is first
    assert len(seen) == 2
    assert seen[1].headers["if-none-match"] == '"abc"'
    assert seen[1].url.params["last_rule_id"] == "r1"
    assert seen[1].url.params["last_rule_version"] == "4"
    assert handler._rules_last_update > 0
    assert handler.rules_fetched == 1


@pytest.mark.asyncio
async def test_304_restarts_the_cache_window() -> None:
    handler, seen = make_handler(
        lambda request: (
            httpx.Response(304)
            if "if-none-match" in request.headers
            else httpx.Response(200, json=rules_json(), headers={"ETag": "e"})
        )
    )
    await poll(handler)
    expire(handler)
    await poll(handler)

    await poll(handler)

    assert len(seen) == 2


@pytest.mark.asyncio
async def test_a_changed_rule_set_replaces_the_cache_and_the_etag() -> None:
    answers = iter(
        [
            httpx.Response(200, json=rules_json("r1", 1), headers={"ETag": "e1"}),
            httpx.Response(200, json=rules_json("r2", 1), headers={"ETag": "e2"}),
            httpx.Response(304),
        ]
    )
    handler, seen = make_handler(lambda request: next(answers))
    await poll(handler)
    expire(handler)
    changed = await poll(handler)
    expire(handler)

    again = await poll(handler)

    assert changed is not None and changed.rule_id == "r2"
    assert again is changed
    assert seen[2].headers["if-none-match"] == "e2"
    assert seen[2].url.params["last_rule_id"] == "r2"


@pytest.mark.asyncio
async def test_saas_that_ignores_the_conditional_keeps_the_v3_2_4_behavior() -> None:
    version = iter([1, 2])
    handler, seen = make_handler(
        lambda request: httpx.Response(200, json=rules_json("r1", next(version)))
    )
    await poll(handler)
    expire(handler)

    second = await poll(handler)

    assert second is not None and second.version == 2
    assert handler._cached_rules is second
    assert handler.rules_fetched == 2
    assert "if-none-match" not in seen[1].headers
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_304_without_held_rules_is_not_retried() -> None:
    handler, seen = make_handler(lambda request: httpx.Response(304))

    assert await poll(handler) is None

    assert len(seen) == 1
