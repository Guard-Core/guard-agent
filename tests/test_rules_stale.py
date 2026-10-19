import logging
from datetime import datetime, timezone

import httpx
import pytest

from guard_agent.models import AgentStatus
from tests.rules_http import fake_clock, make_handler, poll, rules_json

pytestmark = pytest.mark.usefixtures("fresh_agent_singleton")

_HOUR = 3600.0
_T0 = 1_000_000.0


def _saas(up: list[bool]) -> httpx.Response:
    if up[0]:
        return httpx.Response(200, json=rules_json("held-rule", 7, ttl=60))
    return httpx.Response(503, text="down")


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "stale" in r.getMessage().lower()
    ]


@pytest.mark.asyncio
async def test_stale_rules_warn_once_then_hourly_and_still_serve_the_cache(
    caplog: pytest.LogCaptureFixture,
) -> None:
    up = [True]
    handler, _ = make_handler(lambda request: _saas(up))
    clock = [_T0]
    caplog.set_level(logging.DEBUG)
    with fake_clock(clock):
        held = await poll(handler)
        up[0] = False

        clock[0] = _T0 + 30
        assert await poll(handler) is held
        assert _warnings(caplog) == []

        clock[0] = _T0 + 100
        assert await poll(handler) is held
        assert len(_warnings(caplog)) == 1

        clock[0] = _T0 + 200
        await poll(handler)
        clock[0] = _T0 + 100 + _HOUR - 1
        await poll(handler)
        assert len(_warnings(caplog)) == 1

        clock[0] = _T0 + 100 + _HOUR
        assert await poll(handler) is held

    messages = _warnings(caplog)
    assert len(messages) == 2
    assert "held-rule" in messages[0] and "7" in messages[0]
    assert "100" in messages[0]
    assert "enable_dynamic_rules" not in " ".join(messages)


@pytest.mark.asyncio
async def test_first_good_check_after_stale_logs_one_info_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    up = [True]
    handler, _ = make_handler(lambda request: _saas(up))
    clock = [_T0]
    caplog.set_level(logging.DEBUG)
    with fake_clock(clock):
        await poll(handler)
        up[0] = False
        clock[0] = _T0 + 100
        await poll(handler)
        up[0] = True
        clock[0] = _T0 + 200
        await poll(handler)
        clock[0] = _T0 + 300
        await poll(handler)

    infos = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and "recover" in r.getMessage().lower()
    ]
    assert len(infos) == 1
    assert "held-rule" in infos[0]


@pytest.mark.asyncio
async def test_status_reports_stale_rules_and_the_last_successful_check() -> None:
    up = [True]
    handler, _ = make_handler(lambda request: _saas(up))
    clock = [_T0]
    with fake_clock(clock):
        await poll(handler)
        fresh = await handler.get_status()
        up[0] = False
        clock[0] = _T0 + 100
        await poll(handler)
        stale = await handler.get_status()

    assert fresh.rules_stale is False
    assert stale.rules_stale is True
    assert stale.rules_last_checked == datetime.fromtimestamp(_T0, tz=timezone.utc)


@pytest.mark.asyncio
async def test_status_before_any_check_is_not_stale() -> None:
    handler, _ = make_handler(lambda request: httpx.Response(503))

    status = await handler.get_status()

    assert status.rules_stale is False
    assert status.rules_last_checked is None


def test_the_status_payload_without_the_new_fields_is_still_valid() -> None:
    status = AgentStatus(
        timestamp=datetime.now(timezone.utc),
        status="healthy",
        uptime=1.0,
        events_sent=0,
        events_failed=0,
        buffer_size=0,
    )

    assert status.rules_stale is False
    assert status.rules_last_checked is None
