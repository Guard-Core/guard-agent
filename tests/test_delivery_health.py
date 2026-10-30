from __future__ import annotations

import json
import threading
from collections.abc import AsyncGenerator, Generator
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from guard_agent.client import GuardAgentHandler
from guard_agent.models import AgentConfig, SecurityEvent


class Ingest:
    def __init__(self) -> None:
        self.events_status = 503
        self.events_body = b""
        self.event_failures_left = 0


@pytest.fixture
def ingest() -> Generator[tuple[Ingest, int], None, None]:
    state = Ingest()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            status = 201
            body = b""
            if self.path == "/api/v1/events":
                if state.event_failures_left > 0:
                    state.event_failures_left -= 1
                    status = 503
                else:
                    status = state.events_status if state.events_status != 503 else 201
                    body = state.events_body
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, server.server_port
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _event() -> SecurityEvent:
    return SecurityEvent(
        timestamp=datetime.now(timezone.utc),
        event_type="penetration_attempt",
        ip_address="203.0.113.7",
        action_taken="logged",
    )


@pytest.fixture
async def agent(
    ingest: tuple[Ingest, int], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncGenerator[GuardAgentHandler, None]:
    _state, port = ingest
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    GuardAgentHandler._instance = None
    handler = GuardAgentHandler(
        AgentConfig(
            api_key="k1234567890abc",
            endpoint=f"http://127.0.0.1:{port}",
            retry_attempts=0,
            timeout=5,
        )
    )
    await handler.transport.initialize()
    handler._running = True
    yield handler
    await handler.transport.close()
    GuardAgentHandler._instance = None


async def _flush(handler: GuardAgentHandler) -> None:
    handler._events_retry_after = 0.0
    await handler.flush_buffer()


@pytest.mark.asyncio
async def test_three_failed_flushes_then_success_is_healthy_with_nothing_lost(
    ingest: tuple[Ingest, int], agent: GuardAgentHandler
) -> None:
    state, _port = ingest
    state.event_failures_left = 3
    for _ in range(10):
        await agent.send_event(_event())

    for _ in range(3):
        await _flush(agent)
        assert agent.events_failed == 0
    degraded = await agent.get_status()
    assert degraded.status == "degraded"
    assert not await agent.health_check()

    await _flush(agent)

    status = await agent.get_status()
    assert (agent.events_sent, agent.events_failed) == (10, 0)
    assert await agent.buffer.get_buffer_size() == 0
    assert status.status == "healthy"
    assert status.events_failed == 0
    assert status.errors == []
    assert await agent.health_check()


@pytest.mark.asyncio
async def test_one_failed_flush_degrades_status_but_is_not_yet_unhealthy(
    ingest: tuple[Ingest, int], agent: GuardAgentHandler
) -> None:
    state, _port = ingest
    state.event_failures_left = 1
    await agent.send_event(_event())

    await _flush(agent)

    status = await agent.get_status()
    assert status.status == "degraded"
    assert "Delivery is failing: 1 consecutive flush failure(s)" in status.errors
    assert await agent.health_check()


@pytest.mark.asyncio
async def test_events_the_transport_drops_for_good_are_the_failed_ones(
    ingest: tuple[Ingest, int], agent: GuardAgentHandler
) -> None:
    state, _port = ingest
    state.events_status = 422
    state.events_body = json.dumps({"detail": "bad"}).encode()
    for _ in range(4):
        await agent.send_event(_event())

    await _flush(agent)

    assert (agent.events_sent, agent.events_failed) == (0, 4)
    assert await agent.buffer.get_buffer_size() == 0
    assert agent.transport.items_dropped_permanently == 4
