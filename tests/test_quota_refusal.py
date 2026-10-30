from __future__ import annotations

import json
import threading
from collections.abc import AsyncGenerator, Generator
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from guard_agent.client import GuardAgentHandler
from guard_agent.models import AgentConfig, SecurityEvent, SecurityMetric

QUOTA_ERROR = "Event quota exceeded. Upgrade your plan."
BATCH = 10
RETRY_ATTEMPTS = 2


class Saas:
    """The ingestion API as guard-core-app answers it."""

    def __init__(self) -> None:
        self.events_body: dict[str, object] = {}
        self.metrics_body: dict[str, object] = {}
        self.event_posts = 0
        self.metric_posts = 0


@pytest.fixture
def saas() -> Generator[tuple[Saas, int], None, None]:
    state = Saas()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/api/v1/events":
                state.event_posts += 1
                body = json.dumps(state.events_body).encode()
            else:
                state.metric_posts += 1
                body = json.dumps(state.metrics_body).encode()
            self.send_response(200)
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


class Redis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get_key(self, namespace: str, key: str) -> str | None:
        return self.store.get(f"{namespace}:{key}")

    async def set_key(
        self, namespace: str, key: str, value: str, ttl: int | None = None
    ) -> bool:
        self.store[f"{namespace}:{key}"] = value
        return True

    async def delete(self, namespace: str, key: str) -> int:
        return int(self.store.pop(f"{namespace}:{key}", None) is not None)

    async def keys(self, pattern: str) -> list[str]:
        return [k for k in self.store if k.startswith(pattern.rstrip("*"))]

    async def initialize(self) -> None: ...

    def get_connection(self) -> object:
        return self


@pytest.fixture
async def agent(
    saas: tuple[Saas, int], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncGenerator[GuardAgentHandler, None]:
    _state, port = saas
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    GuardAgentHandler._instance = None
    handler = GuardAgentHandler(
        AgentConfig(
            api_key="k1234567890abc",
            endpoint=f"http://127.0.0.1:{port}",
            retry_attempts=RETRY_ATTEMPTS,
            timeout=5,
        )
    )
    await handler.transport.initialize()
    handler._running = True
    yield handler
    await handler.transport.close()
    GuardAgentHandler._instance = None


def _quota_refusal(received: int) -> dict[str, object]:
    return {
        "success": False,
        "events_received": received,
        "events_processed": 0,
        "errors": [QUOTA_ERROR],
        "quota_exceeded": True,
    }


def _event() -> SecurityEvent:
    return SecurityEvent(
        timestamp=datetime.now(timezone.utc),
        event_type="penetration_attempt",
        ip_address="203.0.113.7",
        action_taken="logged",
    )


def _metric() -> SecurityMetric:
    return SecurityMetric(
        timestamp=datetime.now(timezone.utc), metric_type="request_count", value=1.0
    )


async def _flush(handler: GuardAgentHandler) -> None:
    handler._events_retry_after = 0.0
    handler._metrics_retry_after = 0.0
    await handler.flush_buffer()


async def _fill_events(handler: GuardAgentHandler, count: int = BATCH) -> None:
    for _ in range(count):
        await handler.send_event(_event())


@pytest.mark.asyncio
async def test_a_batch_refused_for_quota_is_dropped_after_the_retry_limit(
    saas: tuple[Saas, int], agent: GuardAgentHandler
) -> None:
    state, _port = saas
    state.events_body = _quota_refusal(BATCH)
    await _fill_events(agent)

    for _ in range(RETRY_ATTEMPTS + 3):
        await _flush(agent)

    assert state.event_posts == RETRY_ATTEMPTS + 1
    assert await agent.buffer.get_buffer_size() == 0
    assert (agent.events_sent, agent.events_failed) == (0, BATCH)


@pytest.mark.asyncio
async def test_a_batch_refused_for_quota_is_requeued_until_the_retry_limit(
    saas: tuple[Saas, int], agent: GuardAgentHandler
) -> None:
    state, _port = saas
    state.events_body = _quota_refusal(BATCH)
    await _fill_events(agent)

    for _ in range(RETRY_ATTEMPTS):
        await _flush(agent)

    assert await agent.buffer.get_buffer_size() == BATCH
    assert (agent.events_sent, agent.events_failed) == (0, 0)


@pytest.mark.asyncio
async def test_a_refusal_for_quota_backs_off_between_flushes(
    saas: tuple[Saas, int], agent: GuardAgentHandler
) -> None:
    state, _port = saas
    state.events_body = _quota_refusal(BATCH)
    await _fill_events(agent)

    await agent.flush_buffer()
    await agent.flush_buffer()
    await agent.flush_buffer()

    assert state.event_posts == 1


@pytest.mark.asyncio
async def test_a_flush_the_server_accepts_restarts_the_count(
    saas: tuple[Saas, int], agent: GuardAgentHandler
) -> None:
    state, _port = saas
    state.events_body = _quota_refusal(BATCH)
    await _fill_events(agent)
    for _ in range(RETRY_ATTEMPTS):
        await _flush(agent)
    state.events_body = {"success": True, "events_received": BATCH}
    await _flush(agent)
    assert agent.events_sent == BATCH

    state.events_body = _quota_refusal(BATCH)
    await _fill_events(agent)
    for _ in range(RETRY_ATTEMPTS):
        await _flush(agent)

    assert await agent.buffer.get_buffer_size() == BATCH
    assert agent.events_failed == 0


@pytest.mark.asyncio
async def test_a_failure_that_is_not_a_quota_refusal_restarts_the_count(
    saas: tuple[Saas, int], agent: GuardAgentHandler
) -> None:
    state, _port = saas
    state.events_body = _quota_refusal(BATCH)
    await _fill_events(agent)
    for _ in range(RETRY_ATTEMPTS):
        await _flush(agent)
    state.events_body = {"success": False, "errors": ["Event processing error"]}
    await _flush(agent)

    state.events_body = _quota_refusal(BATCH)
    for _ in range(RETRY_ATTEMPTS):
        await _flush(agent)

    assert await agent.buffer.get_buffer_size() == BATCH
    assert agent.events_failed == 0


@pytest.mark.asyncio
async def test_a_dropped_batch_leaves_nothing_persisted_in_redis(
    saas: tuple[Saas, int], agent: GuardAgentHandler
) -> None:
    state, _port = saas
    state.events_body = _quota_refusal(BATCH)
    redis = Redis()
    await agent.initialize_redis(redis)
    await _fill_events(agent)
    await agent.buffer.stop_auto_flush()
    assert len(redis.store) == BATCH

    for _ in range(RETRY_ATTEMPTS + 1):
        await _flush(agent)

    assert redis.store == {}


@pytest.mark.asyncio
async def test_metrics_refused_for_quota_are_dropped_after_the_retry_limit(
    saas: tuple[Saas, int], agent: GuardAgentHandler
) -> None:
    state, _port = saas
    state.metrics_body = _quota_refusal(0)
    for _ in range(BATCH):
        await agent.send_metric(_metric())

    for _ in range(RETRY_ATTEMPTS + 3):
        await _flush(agent)

    assert state.metric_posts == RETRY_ATTEMPTS + 1
    assert await agent.buffer.get_buffer_size() == 0
    assert (agent.metrics_sent, agent.metrics_failed) == (0, BATCH)


@pytest.mark.asyncio
async def test_events_the_server_dropped_for_their_timestamp_count_as_lost(
    saas: tuple[Saas, int], agent: GuardAgentHandler
) -> None:
    state, _port = saas
    dropped = 4
    state.events_body = {
        "success": True,
        "events_received": BATCH,
        "events_processed": BATCH - dropped,
        "events_dropped_invalid_timestamp": dropped,
    }
    await _fill_events(agent)

    await _flush(agent)

    assert (agent.events_sent, agent.events_failed) == (BATCH - dropped, dropped)
    assert await agent.buffer.get_buffer_size() == 0


@pytest.mark.asyncio
async def test_a_zero_or_non_numeric_drop_count_is_not_a_loss(
    saas: tuple[Saas, int], agent: GuardAgentHandler
) -> None:
    state, _port = saas
    bodies: list[dict[str, object]] = [
        {"success": True, "events_dropped_invalid_timestamp": 0},
        {"success": True, "events_dropped_invalid_timestamp": True},
        {"success": True, "events_dropped_invalid_timestamp": "4"},
    ]
    for body in bodies:
        state.events_body = body
        await _fill_events(agent, 2)
        await _flush(agent)

    assert (agent.events_sent, agent.events_failed) == (6, 0)
