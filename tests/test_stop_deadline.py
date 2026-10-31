from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import AsyncGenerator, Generator
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from guard_agent.client import GuardAgentHandler
from guard_agent.models import AgentConfig, SecurityEvent

BATCH = 5
STOP_TIMEOUT = 1.5
SLACK_SECONDS = 1.5
RETRY_ATTEMPTS = 3
REQUEST_TIMEOUT = 5


class Stub:
    def __init__(self) -> None:
        self.status = 200
        self.posts = 0


@pytest.fixture
def stub() -> Generator[tuple[Stub, int], None, None]:
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state.posts += 1
            body = json.dumps({"success": state.status == 200}).encode()
            self.send_response(state.status)
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


@pytest.fixture
def refused_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def black_hole() -> Generator[int, None, None]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    yield int(sock.getsockname()[1])
    sock.close()


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


def _shutting_down(handler: GuardAgentHandler) -> bool:
    return bool(handler.transport.shutting_down)


def _event() -> SecurityEvent:
    return SecurityEvent(
        timestamp=datetime.now(timezone.utc),
        event_type="penetration_attempt",
        ip_address="203.0.113.7",
        action_taken="logged",
    )


async def _agent(
    port: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncGenerator[GuardAgentHandler, None]:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    GuardAgentHandler._instance = None
    handler = GuardAgentHandler(
        AgentConfig(
            api_key="k1234567890abc",
            endpoint=f"http://127.0.0.1:{port}",
            retry_attempts=RETRY_ATTEMPTS,
            backoff_factor=1.0,
            timeout=REQUEST_TIMEOUT,
            stop_timeout=STOP_TIMEOUT,
        )
    )
    await handler.transport.initialize()
    for _ in range(BATCH):
        await handler.send_event(_event())
    yield handler
    GuardAgentHandler._instance = None


@pytest.mark.asyncio
async def test_stop_against_a_refused_port_returns_within_the_bound(
    refused_port: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async for handler in _agent(refused_port, monkeypatch, tmp_path):
        started = time.monotonic()
        await handler.stop()
        elapsed = time.monotonic() - started

        assert elapsed < STOP_TIMEOUT + SLACK_SECONDS
        assert handler.events_failed == BATCH


@pytest.mark.asyncio
async def test_stop_against_a_black_holed_listener_returns_within_the_bound(
    black_hole: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async for handler in _agent(black_hole, monkeypatch, tmp_path):
        started = time.monotonic()
        await handler.stop()
        elapsed = time.monotonic() - started

        assert elapsed < STOP_TIMEOUT + SLACK_SECONDS
        assert handler.events_failed == BATCH


@pytest.mark.asyncio
async def test_stop_sends_each_batch_once_without_retries(
    stub: tuple[Stub, int], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state, port = stub
    state.status = 503
    async for handler in _agent(port, monkeypatch, tmp_path):
        await handler.stop()

        assert state.posts == 1


@pytest.mark.asyncio
async def test_stop_delivers_the_buffered_events_when_the_endpoint_is_up(
    stub: tuple[Stub, int], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state, port = stub
    async for handler in _agent(port, monkeypatch, tmp_path):
        await handler.stop()

        assert state.posts == 1
        assert (handler.events_sent, handler.events_failed) == (BATCH, 0)


@pytest.mark.asyncio
async def test_unsent_events_stay_in_redis_and_are_not_counted_lost(
    refused_port: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis = Redis()
    async for handler in _agent(refused_port, monkeypatch, tmp_path):
        await handler.initialize_redis(redis)
        for _ in range(BATCH):
            await handler.send_event(_event())
        await handler.buffer.stop_auto_flush()
        persisted = len(redis.store)
        with caplog.at_level(logging.WARNING, logger="guard_agent"):
            await handler.stop()

        stop_warnings = [r for r in caplog.records if "stop():" in r.getMessage()]
        assert len(stop_warnings) == 1
        assert "stay in Redis" in stop_warnings[0].getMessage()
        assert handler.events_failed == 0
        assert persisted > 0
        assert len(redis.store) == persisted


@pytest.mark.asyncio
async def test_unsent_events_without_redis_are_counted_lost_with_one_warning(
    refused_port: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async for handler in _agent(refused_port, monkeypatch, tmp_path):
        with caplog.at_level(logging.WARNING, logger="guard_agent"):
            await handler.stop()

        stop_warnings = [r for r in caplog.records if "stop():" in r.getMessage()]
        assert len(stop_warnings) == 1
        assert "are lost" in stop_warnings[0].getMessage()


@pytest.mark.asyncio
async def test_a_restarted_transport_retries_again(
    stub: tuple[Stub, int], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state, port = stub
    async for handler in _agent(port, monkeypatch, tmp_path):
        await handler.stop()
        assert _shutting_down(handler)

        await handler.transport.initialize()

        assert not _shutting_down(handler)
        assert state.posts == 1


@pytest.mark.asyncio
async def test_stop_does_not_wait_for_an_exhausted_rate_limiter(
    stub: tuple[Stub, int], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state, port = stub
    async for handler in _agent(port, monkeypatch, tmp_path):
        monkeypatch.setattr(
            handler.transport.rate_limiter, "get_retry_after", lambda: 60.0
        )

        async def refuse() -> bool:
            return False

        monkeypatch.setattr(handler.transport.rate_limiter, "acquire", refuse)
        started = time.monotonic()
        await handler.stop()

        assert time.monotonic() - started < STOP_TIMEOUT
        assert state.posts == 0
        assert handler.events_failed == BATCH
