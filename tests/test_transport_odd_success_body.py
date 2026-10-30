from __future__ import annotations

import logging
import threading
import time
from collections.abc import Generator
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from guard_agent.models import AgentConfig, SecurityEvent, SecurityMetric
from guard_agent.transport import HTTPTransport


class Stub:
    def __init__(self) -> None:
        self.status = 200
        self.body = b"{}"
        self.posts: list[str] = []
        self.gets: list[str] = []


@pytest.fixture
def stub() -> Generator[tuple[Stub, int], None, None]:
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, record: list[str]) -> None:
            record.append(self.path)
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            self.wfile.write(state.body)

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._answer(state.posts)

        def do_GET(self) -> None:
            self._answer(state.gets)

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


def _metric() -> SecurityMetric:
    return SecurityMetric(
        timestamp=datetime.now(timezone.utc),
        metric_type="request_count",
        value=1.0,
    )


def _transport(
    port: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_attempts: int = 3,
    backoff_factor: float = 1.0,
) -> HTTPTransport:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = AgentConfig(
        api_key="k1234567890abc",
        endpoint=f"http://127.0.0.1:{port}",
        timeout=5,
        retry_attempts=retry_attempts,
        backoff_factor=backoff_factor,
    )
    return HTTPTransport(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"{}", b"OK", b"", b"[]", b"null"])
async def test_a_2xx_without_a_json_object_is_one_delivery_not_instant_resends(
    stub: tuple[Stub, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    body: bytes,
) -> None:
    state, port = stub
    state.body = body
    transport = _transport(port, tmp_path, monkeypatch)
    await transport.initialize()

    with caplog.at_level(logging.WARNING, logger="guard_agent.transport"):
        events_ok = await transport.send_events([_event()])
        metrics_ok = await transport.send_metrics([_metric()])
        again = await transport.send_events([_event()])
    await transport.close()

    assert (events_ok, metrics_ok, again) == (True, True, True)
    assert state.posts == ["/api/v1/events", "/api/v1/metrics", "/api/v1/events"]
    odd = [r for r in caplog.records if "success body" in r.getMessage()]
    assert len(odd) == 1
    assert odd[0].levelno == logging.WARNING


@pytest.mark.asyncio
async def test_a_json_object_body_is_delivered_without_the_odd_body_warning(
    stub: tuple[Stub, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state, port = stub
    state.body = b'{"success": true, "events_received": 1}'
    transport = _transport(port, tmp_path, monkeypatch)
    await transport.initialize()

    with caplog.at_level(logging.WARNING, logger="guard_agent.transport"):
        delivered = await transport.send_events([_event()])
    await transport.close()

    assert delivered is True
    assert len(state.posts) == 1
    assert not [r for r in caplog.records if "success body" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_partial_failure_body_is_still_a_failure_without_resends(
    stub: tuple[Stub, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, port = stub
    state.body = b'{"success": false, "errors": ["bad"]}'
    transport = _transport(port, tmp_path, monkeypatch)
    await transport.initialize()

    delivered = await transport.send_events([_event()])
    await transport.close()

    assert delivered is False
    assert len(state.posts) == 1


@pytest.mark.asyncio
async def test_a_rules_get_answered_200_without_json_backs_off_between_attempts(
    stub: tuple[Stub, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, port = stub
    state.body = b"OK"
    transport = _transport(
        port, tmp_path, monkeypatch, retry_attempts=2, backoff_factor=0.05
    )
    await transport.initialize()

    started = time.monotonic()
    rules = await transport.fetch_dynamic_rules()
    elapsed = time.monotonic() - started
    await transport.close()

    assert rules is None
    assert len(state.gets) == 3
    assert elapsed >= 0.05 + 0.1 - 0.02
