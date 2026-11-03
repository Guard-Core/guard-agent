import gc

from guard_agent.client import SyncGuardAgentHandler


def stop_every_running_sync_agent() -> None:
    """A SyncGuardAgentHandler runs its own loop thread; one left alive keeps
    flushing to its endpoint (and logging) while later tests run."""
    for candidate in gc.get_objects():
        if (
            isinstance(candidate, SyncGuardAgentHandler)
            and hasattr(candidate, "_thread")
            and candidate._thread.is_alive()
            and not candidate._loop.is_closed()
        ):
            candidate.stop()
