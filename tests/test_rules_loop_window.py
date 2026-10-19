import asyncio
from collections.abc import Iterable
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from tests.rules_http import make_handler, rules_json

pytestmark = pytest.mark.usefixtures("fresh_agent_singleton")

_CLOCK_MODULE = "guard_agent._client_loops.time"


def _saas(request: httpx.Request) -> httpx.Response:
    if request.headers.get("if-none-match") == "e":
        return httpx.Response(304)
    return httpx.Response(200, json=rules_json("r1", 1), headers={"ETag": "e"})


class Timeline:
    """Replays engine calls and agent loop wakes on a controlled clock."""

    def __init__(self, agent_interval: float, wall_offset: float = 0.0) -> None:
        self.handler, self.seen = make_handler(_saas)
        self.handler.config.dynamic_rule_interval = int(agent_interval)
        self.agent_interval = agent_interval
        self.now = 0.0
        self.wall_offset = wall_offset
        self.loop_gets: list[float] = []
        self.engine_gets = 0

    async def run(
        self,
        engine_calls: Iterable[float],
        loop_wakes: Iterable[float],
        loop_first: bool = False,
    ) -> None:
        order = 0 if loop_first else 1
        events = [(t, 1 - order, "engine") for t in engine_calls] + [
            (t, order, "loop") for t in loop_wakes
        ]
        # Only guard_agent's reference is replaced: asyncio itself reads time.monotonic.
        clock = SimpleNamespace(
            monotonic=lambda: self.now,
            time=lambda: 1_000_000.0 + self.now + self.wall_offset,
        )
        with patch(_CLOCK_MODULE, clock):
            for at, _, who in sorted(events):
                self.now = at
                if who == "engine":
                    before = len(self.seen)
                    await self.handler.get_dynamic_rules()
                    self.engine_gets += len(self.seen) - before
                elif self.handler._rules_check_is_due():
                    self.loop_gets.append(at)
                    await self.handler._shared_rules_check()
                await asyncio.sleep(0)


def _every(step: float, start: float, stop: float) -> list[float]:
    count = int((stop - start) / step) + 1
    return [start + step * k for k in range(count)]


@pytest.mark.asyncio
async def test_loop_waking_40ms_before_the_engine_adds_no_get_at_60_over_60() -> None:
    timeline = Timeline(60)

    await timeline.run(
        engine_calls=_every(60, 0, 600),
        loop_wakes=[t - 0.04 for t in _every(60, 60, 600)],
        loop_first=True,
    )

    assert len(timeline.seen) == 11
    assert timeline.loop_gets == []


@pytest.mark.asyncio
async def test_engine_300_over_agent_60_has_one_get_per_engine_interval() -> None:
    timeline = Timeline(60)

    await timeline.run(_every(300, 0, 1800), _every(60, 60, 1800))

    assert all(t < 300 for t in timeline.loop_gets)
    assert len(timeline.loop_gets) <= 300 // 60 - 2
    assert len(timeline.seen) == 7 + len(timeline.loop_gets)


@pytest.mark.asyncio
async def test_two_close_force_updates_cost_loop_gets_within_one_engine_interval() -> (
    None
):
    timeline = Timeline(60)
    engine = sorted({*_every(300, 0, 1500), 610.0, 620.0})

    await timeline.run(engine, _every(60, 60, 1500))

    extras = [t for t in timeline.loop_gets if t > 300]
    assert extras == [720.0, 780.0, 840.0]


@pytest.mark.asyncio
async def test_one_mid_interval_force_update_costs_one_loop_get() -> None:
    timeline = Timeline(60)
    engine = sorted({*_every(300, 0, 1500), 750.0})

    await timeline.run(engine, _every(60, 60, 1500))

    assert [t for t in timeline.loop_gets if t > 300] == [1140.0]


@pytest.mark.asyncio
async def test_a_force_update_at_610_adds_no_loop_get() -> None:
    timeline = Timeline(60)
    engine = sorted({*_every(300, 0, 1500), 610.0})

    await timeline.run(engine, _every(60, 60, 1500))

    assert [t for t in timeline.loop_gets if t > 300] == []


@pytest.mark.asyncio
async def test_when_the_engine_stops_the_loop_polls_within_the_window() -> None:
    timeline = Timeline(60)
    last_engine_call = 600.0

    await timeline.run(_every(300, 0, last_engine_call), _every(60, 60, 1800))

    window = 1.5 * 300
    after = [t for t in timeline.loop_gets if t > last_engine_call]
    assert after
    assert after[0] <= last_engine_call + window + 60


@pytest.mark.asyncio
async def test_a_standalone_agent_polls_once_per_interval() -> None:
    timeline = Timeline(60)

    await timeline.run([], _every(60, 60, 600))

    assert len(timeline.loop_gets) == 10
    assert len(timeline.seen) == 10


@pytest.mark.asyncio
async def test_a_backward_wall_clock_step_does_not_stop_a_standalone_agent() -> None:
    timeline = Timeline(60)
    await timeline.run([], [60.0, 120.0])
    timeline.wall_offset = -3600.0

    await timeline.run([], [180.0, 240.0, 300.0])

    assert timeline.loop_gets == [60.0, 120.0, 180.0, 240.0, 300.0]


@pytest.mark.asyncio
async def test_a_call_that_joins_an_in_flight_request_is_still_stamped() -> None:
    gate = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return _saas(request)

    handler, _ = make_handler(respond)
    handler._running = True
    loop_call = asyncio.ensure_future(handler._rules_check_from_the_loop())
    await asyncio.sleep(0.02)
    joiners = [asyncio.ensure_future(handler.get_dynamic_rules()) for _ in range(2)]
    await asyncio.sleep(0.02)
    gate.set()
    await asyncio.gather(loop_call, *joiners)

    assert len(handler._outside_call_stamps) == 2
