import threading

import pytest

from guard_agent.models import AgentConfig
from tests import thread_guard


@pytest.fixture(autouse=True)
def no_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(thread_guard, "_GRACE_SECONDS", 0.0)


def _park(name: str, daemon: bool) -> tuple[threading.Thread, threading.Event]:
    release = threading.Event()
    thread = threading.Thread(target=release.wait, name=name, daemon=daemon)
    thread.start()
    return thread, release


@pytest.mark.parametrize(
    ("name", "daemon"),
    [
        ("wp683-parked", False),
        ("guard-agent-loop", True),
        ("ThreadPoolExecutor-99_0", True),
        ("asyncio_0", True),
    ],
)
def test_a_thread_started_by_a_test_and_still_alive_is_named(
    name: str, daemon: bool
) -> None:
    thread, release = _park(name, daemon)
    try:
        assert f"{name} (daemon={daemon})" in thread_guard.leaked_threads()
    finally:
        release.set()
        thread.join()


def test_a_thread_that_has_finished_is_not_reported() -> None:
    thread = threading.Thread(target=lambda: None, name="guard-agent-finished")
    thread.start()
    thread.join()
    assert not any("guard-agent-finished" in n for n in thread_guard.leaked_threads())


def test_a_plain_daemon_thread_is_not_reported() -> None:
    thread, release = _park("wp683-daemon", True)
    try:
        assert not any("wp683-daemon" in n for n in thread_guard.leaked_threads())
    finally:
        release.set()
        thread.join()


def test_a_running_sync_agent_loop_thread_is_named_by_the_guard() -> None:
    from guard_agent.client import SyncGuardAgentHandler

    handler = SyncGuardAgentHandler(
        AgentConfig(
            api_key="wp683-test-key", endpoint="http://localhost:8000", project_id="p"
        )
    )
    try:
        assert "guard-agent-loop (daemon=True)" in thread_guard.leaked_threads()
    finally:
        handler.stop()
    assert not any("guard-agent" in n for n in thread_guard.leaked_threads())
