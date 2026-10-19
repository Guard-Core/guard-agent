import asyncio
from unittest.mock import patch

import httpx
import pytest

from guard_agent.client import GuardAgentHandler
from tests.rules_http import fake_clock, make_handler, poll, rules_json

pytestmark = pytest.mark.usefixtures("fresh_agent_singleton")

_HOUR = 3600.0


def _conditional_saas(version: int = 1) -> httpx.Response:
    return httpx.Response(200, json=rules_json("r1", version), headers={"ETag": "e"})


@pytest.mark.asyncio
async def test_every_call_checks_the_saas_even_inside_the_ttl() -> None:
    handler, seen = make_handler(
        lambda request: (
            httpx.Response(304)
            if "if-none-match" in request.headers
            else _conditional_saas()
        )
    )
    first = await poll(handler)

    second = await poll(handler)
    third = await poll(handler)

    assert second is first and third is first
    assert len(seen) == 3
    assert seen[2].headers["if-none-match"] == "e"


@pytest.mark.asyncio
async def test_concurrent_calls_share_one_request_and_its_result() -> None:
    gate = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return _conditional_saas()

    handler, seen = make_handler(respond)
    calls = [asyncio.ensure_future(handler.get_dynamic_rules()) for _ in range(5)]
    await asyncio.sleep(0.05)
    gate.set()

    results = await asyncio.gather(*calls)

    assert len(seen) == 1
    assert all(result is results[0] for result in results)
    assert results[0] is not None


@pytest.mark.asyncio
async def test_a_call_after_the_shared_request_finished_makes_a_new_one() -> None:
    handler, seen = make_handler(lambda request: _conditional_saas())
    await poll(handler)
    await asyncio.sleep(0)

    await poll(handler)

    assert len(seen) == 2


@pytest.mark.asyncio
async def test_a_cancelled_caller_does_not_cancel_the_shared_request() -> None:
    gate = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return _conditional_saas()

    handler, seen = make_handler(respond)
    cancelled = asyncio.ensure_future(handler.get_dynamic_rules())
    survivor = asyncio.ensure_future(handler.get_dynamic_rules())
    await asyncio.sleep(0.05)
    cancelled.cancel()
    gate.set()

    assert await survivor is not None
    assert len(seen) == 1


async def _run_rules_loop(
    handler: GuardAgentHandler, clock: list[float], wakes: int, advance: float
) -> None:
    handler._running = True
    remaining = wakes

    async def fake_sleep(_: float) -> None:
        nonlocal remaining
        if remaining == 0:
            handler._running = False
            return
        remaining -= 1
        clock[0] += advance

    with patch("guard_agent._client_loops.asyncio.sleep", fake_sleep):
        await handler._rules_loop()


@pytest.mark.asyncio
async def test_rules_loop_skips_right_after_an_outside_call() -> None:
    handler, seen = make_handler(lambda request: _conditional_saas())
    handler.config.dynamic_rule_interval = 300
    clock = [1000.0]
    with fake_clock(clock):
        await poll(handler)
        clock[0] += 10
        await _run_rules_loop(handler, clock, wakes=1, advance=100)

    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_standalone_agent_still_polls_every_interval() -> None:
    handler, seen = make_handler(lambda request: _conditional_saas())
    handler.config.dynamic_rule_interval = 300
    clock = [1000.0]
    with fake_clock(clock):
        await _run_rules_loop(handler, clock, wakes=3, advance=300)

    assert len(seen) == 3


@pytest.mark.asyncio
async def test_a_dashboard_change_reaches_the_engine_within_one_engine_interval() -> (
    None
):
    version = [1]

    def respond(request: httpx.Request) -> httpx.Response:
        sent = request.url.params.get("last_rule_version")
        if sent == str(version[0]):
            return httpx.Response(304)
        return httpx.Response(
            200, json=rules_json("r1", version[0]), headers={"ETag": f"e{version[0]}"}
        )

    handler, seen = make_handler(respond)
    handler.config.dynamic_rule_interval = 300
    clock = [0.0]
    seen_by_engine: list[tuple[float, int]] = []
    changed_at = 130.0

    async def engine_call() -> None:
        rules = await handler.get_dynamic_rules()
        assert rules is not None
        seen_by_engine.append((clock[0], rules.version))

    async def fake_sleep(_: float) -> None:
        if clock[0] >= 600:
            handler._running = False
            return
        for _step in range(5):
            clock[0] += 60
            if clock[0] > changed_at:
                version[0] = 2
            await engine_call()

    handler._running = True
    with (
        fake_clock(clock),
        patch("guard_agent._client_loops.asyncio.sleep", fake_sleep),
    ):
        await handler._rules_loop()

    first_v2 = min(at for at, seen_version in seen_by_engine if seen_version == 2)
    assert first_v2 - changed_at <= 60
    assert len(seen) == len(seen_by_engine)
