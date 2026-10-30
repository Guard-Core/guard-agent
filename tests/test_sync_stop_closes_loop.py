from __future__ import annotations

import gc
import sys
import threading
from collections.abc import Generator
from datetime import datetime, timezone

import pytest

from guard_agent.client import GuardAgentHandler, SyncGuardAgentHandler
from guard_agent.models import AgentConfig, SecurityEvent


@pytest.fixture(autouse=True)
def reset_singletons() -> Generator[None, None, None]:
    GuardAgentHandler._instance = None
    SyncGuardAgentHandler._instance = None
    yield
    GuardAgentHandler._instance = None
    SyncGuardAgentHandler._instance = None


@pytest.mark.filterwarnings("error::ResourceWarning")
def test_stop_closes_its_event_loop_so_nothing_is_left_to_warn(
    agent_config: AgentConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    raised: list[object] = []

    def record(unraisable: sys.UnraisableHookArgs) -> None:
        raised.append(unraisable.exc_value)

    monkeypatch.setattr(sys, "unraisablehook", record)
    handler = SyncGuardAgentHandler(agent_config)
    handler.start()
    loop = handler._loop

    handler.stop()

    assert loop.is_closed()
    SyncGuardAgentHandler._instance = None
    del handler
    gc.collect()
    assert [r for r in raised if isinstance(r, ResourceWarning)] == []


class _StuckThread(threading.Thread):
    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float | None = None) -> None:
        return None


def test_a_loop_whose_thread_did_not_stop_is_left_open(
    agent_config: AgentConfig,
) -> None:
    handler = SyncGuardAgentHandler(agent_config)
    real_thread = handler._thread
    handler._thread = _StuckThread()

    handler.stop()

    assert not handler._loop.is_closed()
    real_thread.join(timeout=5)
    handler._loop.close()


def test_a_handler_built_after_stop_is_a_new_working_agent(
    agent_config: AgentConfig,
) -> None:
    from unittest.mock import AsyncMock

    first = SyncGuardAgentHandler(agent_config)
    first.start()
    first.stop()

    second = SyncGuardAgentHandler(agent_config)
    try:
        assert second is not first
        assert second._inner is not first._inner
        assert second._thread.is_alive()
        transport = AsyncMock()
        transport.send_events = AsyncMock(return_value=True)
        transport.send_metrics = AsyncMock(return_value=True)
        second._inner.transport = transport
        second.start()
        second.send_event(
            SecurityEvent(
                timestamp=datetime.now(timezone.utc),
                event_type="ip_banned",
                ip_address="1.2.3.4",
                action_taken="banned",
            )
        )
        second.flush_buffer()
        assert len(transport.send_events.await_args.args[0]) == 1
    finally:
        second.stop()


def _sync_singleton() -> SyncGuardAgentHandler | None:
    return SyncGuardAgentHandler._instance


def test_a_handler_that_was_replaced_does_not_release_its_successor(
    agent_config: AgentConfig,
) -> None:
    first = SyncGuardAgentHandler(agent_config)
    first.start()
    SyncGuardAgentHandler._instance = None
    GuardAgentHandler._instance = None
    second = SyncGuardAgentHandler(agent_config)
    try:
        first.stop()

        assert _sync_singleton() is second
        assert GuardAgentHandler._instance is second._inner
    finally:
        second.stop()
