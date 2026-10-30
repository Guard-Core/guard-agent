from __future__ import annotations

import gc
import sys
import threading
from collections.abc import Generator

import pytest

from guard_agent.client import GuardAgentHandler, SyncGuardAgentHandler
from guard_agent.models import AgentConfig


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
